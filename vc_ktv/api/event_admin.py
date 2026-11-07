"""Event lifecycle and archive administration; preserve transition semantics."""

import logging
import shutil
from collections.abc import Callable

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel, Field

from ..autoprep import AutoPrep


class EventCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    code: str | None = None


def create_event_admin_router(
    *,
    events,
    require_admin,
    require_global_admin,
    export_event_songs,
    get_worker: Callable[[], AutoPrep],
    publish_worker: Callable[[AutoPrep], None],
    probe_media,
    netease_login,
    stop_manual_renders,
    is_running: Callable[[], bool],
    auto_prepare: bool,
    rendering,
) -> APIRouter:
    router = APIRouter()

    def bail(status: int, message: str):
        raise HTTPException(status_code=status, detail=message)

    @router.post("/api/admin/events/{event_id}/release-media")
    def release_event_media(event_id: str, vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        with events.lock:
            events.closed_folder(event_id)
        csv_data = export_event_songs(event_id, vc_admin).body.decode("utf-8")
        return events.release_media(event_id, csv_data)

    @router.delete("/api/admin/events/{event_id}")
    def delete_event_record(event_id: str, vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        return events.delete_closed(event_id)

    @router.post("/api/admin/events")
    def create_event(body: EventCreate, vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        with events.lock:
            entry = events.create(body.name, body.code)
            worker = AutoPrep(events.active(), probe_media)
            worker.netease_cookies = netease_login.cookies
            worker.cancel_external_tasks = stop_manual_renders
            publish_worker(worker)
            if is_running() and auto_prepare:
                # A new event has no unfinished preparation to replay.
                # A failing thread must not strand the published event.
                worker.resume_after_failed_stop()
                if not worker.workers:
                    # The room is already committed. Report degraded worker
                    # availability via /api/health instead of returning a 503
                    # that makes the operator think room creation failed.
                    logging.getLogger(__name__).error(
                        "Event created, but no AutoPrep worker could start"
                    )
        return entry

    @router.post("/api/admin/events/close")
    def close_event(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        worker = get_worker()
        with events.lock:
            if not events.active_id:
                bail(409, "没有活动场次")
            # Block new readers before waiting on a concurrent Store snapshot;
            # an event close must not leave new requests entering indefinitely.
            events.transitioning = True
            try:
                # Wait without holding Events.lock so new requests see 409,
                # then inspect playback only after existing readers drain.
                if not events.idle.wait_for(lambda: events.inflight == 0, timeout=10):
                    bail(409, "仍有请求正在处理，请稍后重试")
                target = events.active()
                # Stop claims first, cancel active subprocess trees, then join
                # workers. Never close a SQLite connection used by a live thread.
                worker.stop_signal.set()
                with worker.active_tasks_lock:
                    active_ids = list(worker.active_tasks)
                try:
                    for song_id in active_ids:
                        worker.cancel_song(song_id)
                    stop_manual_renders()
                    if not worker.stop() or rendering.is_set():
                        bail(409, "后台任务尚未退出，请稍后重试")
                    # TemporaryDirectory normally handles this; remove leftovers
                    # from interrupted work only after all workers have exited.
                    for temporary in target.root.glob("vc-ktv-[0-9]*-*"):
                        if temporary.is_dir() and not temporary.is_symlink():
                            shutil.rmtree(temporary, ignore_errors=True)
                    with target.lock:
                        before = target.rows(
                            "SELECT id,status,played_at,archive_order FROM songs"
                        )
                        order = target.close_song_order()
                        try:
                            target.db.executemany(
                                "UPDATE songs SET archive_order=? WHERE id=?",
                                [(i, sid) for i, sid in enumerate(order, 1)],
                            )
                            target.db.execute(
                                "UPDATE songs SET status='closed_interrupted' "
                                "WHERE status='playing'"
                            )
                            target.db.execute(
                                "UPDATE songs SET status='closed_unplayed' "
                                "WHERE status IN ('waiting','ready','rendering')"
                            )
                            target.db.commit()
                        except BaseException:
                            target.db.rollback()
                            raise
                    try:
                        return events.close()
                    except BaseException:
                        if events.active_id and events.current_store is target:
                            with target.lock:
                                target.db.executemany(
                                    "UPDATE songs SET status=?,played_at=?,archive_order=? WHERE id=?",
                                    [
                                        (
                                            row["status"],
                                            row["played_at"],
                                            row["archive_order"],
                                            row["id"],
                                        )
                                        for row in before
                                    ],
                                )
                                target.db.commit()
                        raise
                except BaseException:
                    if is_running() and auto_prepare and events.active_id:
                        if any(thread.is_alive() for thread in worker.workers):
                            # Continue surviving threads and replenish exited ones.
                            worker.resume_after_failed_stop()
                        else:
                            # Python threads cannot be restarted. Replace the
                            # stopped worker with the same event and settings.
                            resumed = AutoPrep(
                                events.active(),
                                worker.probe,
                                download=worker.download,
                                enhance=worker.enhance,
                                max_workers=worker.max_workers,
                                gpu_alignment=worker.gpu_alignment,
                            )
                            # No preparation task is running: failed close
                            # only needs a new worker pool, not startup replay.
                            resumed.resume_after_failed_stop()
                            worker = resumed
                            worker.netease_cookies = netease_login.cookies
                            worker.cancel_external_tasks = stop_manual_renders
                            publish_worker(resumed)
                    raise
            finally:
                events.transitioning = False

    return router
