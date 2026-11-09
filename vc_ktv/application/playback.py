"""Playback actions shared by administrator and primary-TV HTTP routes."""

import sqlite3
from pathlib import Path

from vc_ktv.application.errors import bail
from vc_ktv.core.store import time_now


class PlaybackService:
    def __init__(self, store, orders):
        self.store = store
        self.orders = orders

    def cancel_song(self, song_id: int, *, guest_id: str | None = None) -> dict:
        """Cancel an unstarted song in one transaction before stopping its worker."""
        with self.store.lock:
            if guest_id is not None:
                row = self.store.db.execute(
                    "SELECT requester FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if not row:
                    bail(404, "歌曲不存在")
                if row["requester"] != guest_id:
                    bail(403, "只能取消自己点的歌曲")
            try:
                updated = self.store.db.execute(
                    "UPDATE songs SET status='cancelled' WHERE id=? "
                    "AND status IN ('waiting','ready','rendering')",
                    (song_id,),
                )
                if not updated.rowcount:
                    self.store.db.rollback()
                    bail(
                        409,
                        "歌曲已经开始播放，无法取消"
                        if guest_id is not None
                        else "只能取消尚未播放的歌曲",
                    )
                self.store.db.commit()
            except sqlite3.Error:
                self.store.db.rollback()
                raise
        return {"ok": True}

    def claim_next(self) -> None:
        """Claim without interrupting a player; caller keeps lock through its view."""
        with self.store.lock:
            self.store.claim_next()

    def finish(self, song_id: int, *, skipped: bool = False) -> dict:
        with self.store.lock:
            ready = self.store.finish_current(song_id, skipped=skipped)
            if ready is None:
                bail(409, "当前播放已变化")
            return {"ok": True, "finished": song_id, "next_ready": ready}

    def change_mode(self, mode: str) -> dict:
        with self.store.lock:
            if self.store.playback_mode() != mode and self.store.current():
                bail(409, "请先唱完或跳过当前歌曲，再切换播放模式")
            try:
                self.store.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('playback_mode',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (mode,),
                )
                self.store.db.commit()
            except sqlite3.Error:
                self.store.db.rollback()
                raise
        return {"mode": mode}

    def recently_played(self) -> dict:
        with self.store.lock:
            return {
                "songs": self.store.rows(
                    """SELECT s.id,s.bvid,s.source_type,s.source_id,s.title,s.duration,
                          s.page,s.played_at,s.replay_of,g.name AS nickname
                   FROM songs s JOIN guests g ON g.id=s.requester
                   WHERE s.status='played' ORDER BY s.played_at DESC,s.id DESC
                   LIMIT 50"""
                )
            }

    def _set_next_priority(self, song_id: int) -> None:
        """Reserve the only next-song slot inside the caller's SQL transaction."""
        self.store.db.execute("UPDATE songs SET priority_next=0 WHERE priority_next=1")
        self.store.db.execute("UPDATE songs SET priority_next=1 WHERE id=?", (song_id,))

    def pin_next(self, song_id: int) -> dict:
        with self.store.lock:
            song = next((s for s in self.store.songs() if s["id"] == song_id), None)
            if not song:
                if not self.store.db.execute(
                    "SELECT 1 FROM songs WHERE id=?", (song_id,)
                ).fetchone():
                    bail(404, "歌曲不存在")
                bail(409, "只能置顶尚未播放且已可播放的歌曲")
            if not self.store.playable(song, self.store.settings()):
                bail(409, "歌曲尚未准备好，不能安排为下一首")
            try:
                self._set_next_priority(song_id)
                self.store.db.commit()
            except sqlite3.Error:
                self.store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "priority_next": True}

    def schedule_replay(
        self, song_id: int, *, current_only: bool = False, guest_id: str | None = None
    ) -> dict:
        """Guest replays use fair turns; admin/TV replays occupy the next slot."""
        with self.store.lock:
            if guest_id is not None:
                self.orders.check_new_order(guest_id)
            current = self.store.current() if current_only else None
            if current_only and (not current or current["id"] != song_id):
                bail(409, "当前歌曲已发生变化")
            row = self.store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if current_only:
                allowed_statuses = ("playing",)
            elif guest_id is not None:
                allowed_statuses = ("played", "skipped")
            else:
                allowed_statuses = ("played",)
            if (
                row["status"] not in allowed_statuses
                or row["prep_state"] not in ("done", "manual", "original")
                or not self.store.replay_assets(row).get("media_file")
            ):
                bail(409, "只能重唱当前正在播放或本场已唱完的成片")
            if current_only:
                # Repeated TV taps must reuse the existing replay request.
                existing = self.store.db.execute(
                    "SELECT id FROM songs WHERE replay_of=? AND status='ready' ORDER BY id LIMIT 1",
                    (song_id,),
                ).fetchone()
                if existing:
                    try:
                        self._set_next_priority(existing["id"])
                        self.store.db.commit()
                    except sqlite3.Error:
                        self.store.db.rollback()
                        raise
                    return {
                        "id": existing["id"],
                        "status": "ready",
                        "priority_next": True,
                    }
            asset = self.store.replay_assets(row)
            media = self.store.media / Path(asset.get("media_file") or "").name
            if not media.is_file():
                bail(404, "成片已丢失")
            # Pausing new orders never prevents an administrator from replaying.
            self.orders.check_room_capacity()
            # Virtual replays resolve media through replay_of without duplicating files.
            try:
                cur = self.store.db.execute(
                    """INSERT INTO songs(
                        bvid,page,title,uploader,cover,duration,requester,requested_at,
                        status,lyrics,lyric_choice,lyric_source,prep_state,
                        skip_subtitles,priority_next,replay_of,source_type,source_id,
                        lyric_timing_mode,provider_lrc,source_lrc_raw,lyric_offset_ms
                    ) VALUES(?,?,?,?,?,?,?,?,'ready',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["bvid"],
                        row["page"],
                        row["title"],
                        row["uploader"],
                        row["cover"],
                        row["duration"],
                        guest_id if guest_id is not None else row["requester"],
                        time_now(),
                        row["lyrics"],
                        row["lyric_choice"],
                        row["lyric_source"],
                        row["prep_state"],
                        int(
                            bool(
                                row["skip_subtitles"] or row["prep_state"] == "original"
                            )
                        ),
                        0,
                        song_id,
                        row["source_type"],
                        row["source_id"],
                        row["lyric_timing_mode"],
                        row["provider_lrc"],
                        row["source_lrc_raw"],
                        row["lyric_offset_ms"],
                    ),
                )
                new_id = cur.lastrowid
                if guest_id is None:
                    self._set_next_priority(new_id)
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise
        return {"id": new_id, "status": "ready", "priority_next": guest_id is None}
