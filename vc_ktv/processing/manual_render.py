"""Single-slot manual subtitle rendering with event-scoped publication and cancellation."""

import logging
import secrets
import sqlite3
import tempfile
import threading
from pathlib import Path

from vc_ktv.application.common import bail
from vc_ktv.core.store import Store
from vc_ktv.infra.process_cancel import terminate_thread_children
from vc_ktv.rendering.karaoke import render_video, write_ass

log = logging.getLogger(__name__)


class ManualRenderManager:
    def __init__(self, store, events):
        self.store = store
        self.events = events
        self.slot = threading.BoundedSemaphore(1)
        self.rendering = threading.Event()
        self.threads: dict[int, int] = {}
        self.lock = threading.RLock()

    def stop_all(self) -> None:
        with self.lock:
            for tid in list(self.threads.values()):
                terminate_thread_children(tid)

    def stop_song(self, song_id: int) -> None:
        with self.lock:
            if tid := self.threads.get(song_id):
                terminate_thread_children(tid)

    def render_worker(
        self,
        target_store: Store,
        song_id: int,
        source: Path,
        lyrics: str,
        duration: float,
        lead: float,
    ) -> None:
        """Keep the Store of the event that originally launched this render."""
        destination = target_store.media / f"{song_id}-ktv-{secrets.token_hex(8)}.mp4"
        with self.lock:
            self.threads[song_id] = threading.get_native_id()
        try:
            with target_store.lock:
                current = target_store.db.execute(
                    "SELECT status FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if not current or current["status"] != "rendering":
                    return
            render_video(source, lyrics, destination, duration, lead)
            with target_store.lock:
                row = target_store.db.execute(
                    "SELECT media_file,status FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if row and row["status"] == "rendering":
                    try:
                        target_store.db.execute(
                            "UPDATE songs SET status='ready',media_file=?,render_error='' WHERE id=?",
                            (destination.name, song_id),
                        )
                        target_store.db.commit()
                    except BaseException:
                        # Roll back the media pointer before deleting the output.
                        target_store.db.rollback()
                        raise
                    # The completed DB pointer must survive an old-video cleanup error.
                    if row["media_file"] and row["media_file"] != source.name:
                        try:
                            (target_store.media / row["media_file"]).unlink(
                                missing_ok=True
                            )
                        except OSError as error:
                            log.warning(
                                "Song #%s obsolete manual render cleanup failed: %s",
                                song_id,
                                error,
                            )
                else:
                    destination.unlink(missing_ok=True)
        except Exception as error:
            destination.unlink(missing_ok=True)
            with target_store.lock:
                for attempt in range(2):
                    try:
                        target_store.db.execute(
                            "UPDATE songs SET status='ready',render_error=? WHERE id=? AND status='rendering'",
                            (f"制作失败：{type(error).__name__}", song_id),
                        )
                        target_store.db.commit()
                        break
                    except sqlite3.Error:
                        target_store.db.rollback()
                        log.exception(
                            "Manual render failure writeback failed for song #%s (attempt %s)",
                            song_id,
                            attempt + 1,
                        )
        finally:
            with self.lock:
                self.threads.pop(song_id, None)
            self.rendering.clear()
            self.slot.release()

    def start(self, song_id: int, lead: float) -> dict:
        if not self.slot.acquire(blocking=False):
            bail(409, "已有歌曲正在制作，请稍后再试")
        try:
            with self.store.lock:
                row = self.store.db.execute(
                    "SELECT * FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if not row:
                    bail(404, "歌曲不存在")
                asset = self.store.replay_assets(row)
                if row["status"] != "ready" or not asset.get("source_file"):
                    bail(409, "请先上传可用视频")
                source = self.store.media / Path(asset["source_file"]).name
                if not source.is_file():
                    bail(404, "原视频不存在，请重新上传")
                try:
                    with tempfile.TemporaryDirectory(prefix="vc-ktv-validate-") as tmp:
                        write_ass(
                            row["lyrics"],
                            Path(tmp) / "lyrics.ass",
                            row["duration"],
                            lead,
                        )
                except ValueError as error:
                    bail(400, str(error))
                try:
                    self.store.db.execute(
                        "UPDATE songs SET status='rendering',render_error='' WHERE id=?",
                        (song_id,),
                    )
                    self.store.db.commit()
                except BaseException:
                    self.store.db.rollback()
                    raise
                self.rendering.set()
                try:
                    thread = threading.Thread(
                        target=self.render_worker,
                        args=(
                            self.events.active(),
                            song_id,
                            source,
                            row["lyrics"],
                            row["duration"],
                            lead,
                        ),
                        daemon=True,
                    )
                    thread.start()
                except BaseException:
                    # No worker started to clear the flag or release the slot.
                    self.rendering.clear()
                    self.store.db.execute(
                        "UPDATE songs SET status='ready' WHERE id=? AND status='rendering'",
                        (song_id,),
                    )
                    self.store.db.commit()
                    raise
                return {"id": song_id, "status": "rendering", "lead": lead}
        except BaseException:
            self.slot.release()
            raise
