"""Event transitions and AutoPrep lifecycle shared by the app and HTTP adapter."""

import asyncio
import logging
import shutil

from vc_ktv.application.common import bail
from vc_ktv.processing.worker import AutoPrep


class EventLifecycle:
    def __init__(
        self, events, worker, manual, netease_login, probe_media, auto_prepare: bool
    ):
        self.events = events
        self.worker = worker
        self.manual = manual
        self.netease_login = netease_login
        self.probe_media = probe_media
        self.auto_prepare = auto_prepare
        self.running = False
        self.app_state = None

    def bind_state(self, state) -> None:
        """Keep the existing app.state.autoprep compatibility/inspection seam."""
        self.app_state = state

    def publish_worker(self, worker: AutoPrep) -> None:
        self.worker = worker
        if self.app_state is not None:
            self.app_state.autoprep = worker

    def stop_song(self, song_id: int) -> None:
        self.worker.cancel_song(song_id)
        self.manual.stop_song(song_id)

    async def monitor_workers(self) -> None:
        """Repair an incomplete pool after individual worker failures."""
        while True:
            await asyncio.sleep(10)
            with self.events.lock:
                if (
                    not self.running
                    or not self.events.active_id
                    or self.events.transitioning
                ):
                    continue
                worker = self.worker
                live = sum(thread.is_alive() for thread in worker.workers)
                if live >= worker.max_workers + worker.fast_workers:
                    continue
                try:
                    if live:
                        worker.resume_after_failed_stop()
                    else:
                        worker.start()
                except Exception:
                    logging.getLogger(__name__).exception("AutoPrep recovery failed")

    def create(self, name: str, code: str | None) -> dict:
        with self.events.lock:
            entry = self.events.create(name, code)
            worker = AutoPrep(self.events.active(), self.probe_media)
            worker.netease_cookies = self.netease_login.cookies
            worker.cancel_external_tasks = self.manual.stop_all
            self.publish_worker(worker)
            if self.running and self.auto_prepare:
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

    def close(self) -> dict:
        worker = self.worker
        with self.events.lock:
            if not self.events.active_id:
                bail(409, "没有活动场次")
            # Block new readers before waiting on a concurrent Store snapshot;
            # an event close must not leave new requests entering indefinitely.
            self.events.transitioning = True
            try:
                # Wait without holding Events.lock so new requests see 409,
                # then inspect playback only after existing readers drain.
                if not self.events.idle.wait_for(
                    lambda: self.events.inflight == 0, timeout=10
                ):
                    bail(409, "仍有请求正在处理，请稍后重试")
                target = self.events.active()
                # Stop claims first, cancel active subprocess trees, then join
                # workers. Never close a SQLite connection used by a live thread.
                worker.stop_signal.set()
                with worker.active_tasks_lock:
                    active_ids = list(worker.active_tasks)
                try:
                    for song_id in active_ids:
                        worker.cancel_song(song_id)
                    self.manual.stop_all()
                    if not worker.stop() or self.manual.rendering.is_set():
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
                        return self.events.close()
                    except BaseException:
                        if (
                            self.events.active_id
                            and self.events.current_store is target
                        ):
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
                    if self.running and self.auto_prepare and self.events.active_id:
                        if any(thread.is_alive() for thread in worker.workers):
                            # Continue surviving threads and replenish exited ones.
                            worker.resume_after_failed_stop()
                        else:
                            # Python threads cannot be restarted. Replace the
                            # stopped worker with the same event and settings.
                            resumed = AutoPrep(
                                self.events.active(),
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
                            worker.netease_cookies = self.netease_login.cookies
                            worker.cancel_external_tasks = self.manual.stop_all
                            self.publish_worker(resumed)
                    raise
            finally:
                self.events.transitioning = False
