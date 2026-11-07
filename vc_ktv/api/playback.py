"""Playback progression, fair replay requests and admin queue controls."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException

from vc_ktv.application.schemas import Finish, PlaybackMode
from vc_ktv.core.store import time_now


def create_playback_router(
    *,
    store,
    require_admin,
    require_user,
    active_view,
    check_new_order,
    check_room_capacity,
):
    router = APIRouter()

    def bail(status: int, message: str):
        raise HTTPException(status_code=status, detail=message)

    def next_impl():
        with store.lock:
            store.claim_next()
            return {"active": active_view()}

    def finish_impl(body: Finish):
        with store.lock:
            next_ready = store.finish_current(
                body.song_id, skipped=body.reason == "skip"
            )
            if next_ready is None:
                bail(409, "当前播放已变化")
            return {"ok": True, "finished": body.song_id, "next_ready": next_ready}

    @router.put("/api/admin/playback-mode")
    def change_playback_mode(
        body: PlaybackMode, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() != body.mode and store.current():
                bail(409, "请先唱完或跳过当前歌曲，再切换播放模式")
            try:
                store.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('playback_mode',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (body.mode,),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"mode": body.mode}

    @router.get("/api/admin/played")
    def recently_played(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            return {
                "songs": store.rows(
                    """SELECT s.id,s.bvid,s.source_type,s.source_id,s.title,s.duration,
                          s.page,s.played_at,s.replay_of,g.name AS nickname
                   FROM songs s JOIN guests g ON g.id=s.requester
                   WHERE s.status='played' ORDER BY s.played_at DESC,s.id DESC
                   LIMIT 50"""
                )
            }

    @router.post("/api/admin/songs/{song_id}/pin-next")
    def pin_next_song(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            song = next((s for s in store.songs() if s["id"] == song_id), None)
            if not song:
                if not store.db.execute(
                    "SELECT 1 FROM songs WHERE id=?", (song_id,)
                ).fetchone():
                    bail(404, "歌曲不存在")
                bail(409, "只能置顶尚未播放且已可播放的歌曲")
            if not store.playable(song, store.settings()):
                bail(409, "歌曲尚未准备好，不能安排为下一首")
            # A single next-song slot: replacing an earlier pin or replay is atomic.
            try:
                store.db.execute(
                    "UPDATE songs SET priority_next=0 WHERE priority_next=1"
                )
                store.db.execute(
                    "UPDATE songs SET priority_next=1 WHERE id=?", (song_id,)
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "priority_next": True}

    def schedule_replay(song_id: int, *, current_only=False, guest_id=None):
        # A guest replay is a new ordinary fair-turn request. Admin/TV
        # replays preserve their existing next-song priority behavior.
        with store.lock:
            if guest_id is not None:
                check_new_order(guest_id)
            if current_only and (
                not store.current() or store.current()["id"] != song_id
            ):
                bail(409, "当前歌曲已发生变化")
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if (
                row["status"]
                not in (
                    ("playing",)
                    if current_only
                    else ("played", "skipped")
                    if guest_id is not None
                    else ("played",)
                )
                or row["prep_state"] not in ("done", "manual", "original")
                or not store.replay_assets(row).get("media_file")
            ):
                bail(409, "只能重唱当前正在播放或本场已唱完的成片")
            if current_only:
                # Repeated TV taps must never create multiple replay requests.
                existing = store.db.execute(
                    "SELECT id FROM songs WHERE replay_of=? AND status='ready' ORDER BY id LIMIT 1",
                    (song_id,),
                ).fetchone()
                if existing:
                    try:
                        store.db.execute(
                            "UPDATE songs SET priority_next=0 WHERE priority_next=1"
                        )
                        store.db.execute(
                            "UPDATE songs SET priority_next=1 WHERE id=?",
                            (existing["id"],),
                        )
                        store.db.commit()
                    except sqlite3.Error:
                        store.db.rollback()
                        raise
                    return {
                        "id": existing["id"],
                        "status": "ready",
                        "priority_next": True,
                    }
            asset = store.replay_assets(row)
            media = store.media / Path(asset.get("media_file") or "").name
            if not media.is_file():
                bail(404, "成片已丢失")
            # Admin replay still works during pause, but cannot exceed the
            # same global unfinished-song capacity.
            check_room_capacity()
            # Virtual replay: retain only the relationship. Source and video
            # paths are resolved on demand, including repeated replays.
            try:
                if guest_id is None:
                    store.db.execute(
                        "UPDATE songs SET priority_next=0 WHERE priority_next=1"
                    )
                cur = store.db.execute(
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
                        int(guest_id is None),
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
                store.db.commit()
            except BaseException:
                store.db.rollback()
                raise
        return {"id": new_id, "status": "ready", "priority_next": guest_id is None}

    @router.post("/api/songs/{song_id}/replay")
    def replay_own_turn(song_id: int, vc_guest: str | None = Cookie(default=None)):
        user = require_user(vc_guest)
        return schedule_replay(song_id, guest_id=user["id"])

    @router.post("/api/admin/songs/{song_id}/replay")
    def replay_song(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return schedule_replay(song_id)

    @router.post("/api/admin/songs/{song_id}/replay-next")
    def replay_current(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "电视模式请使用主屏遥控器")
            return schedule_replay(song_id, current_only=True)

    @router.post("/api/admin/next")
    def next_song(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "当前由电视主屏播放")
            return next_impl()

    @router.post("/api/admin/finish")
    def finish_song(body: Finish, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "当前由电视主屏播放")
            return finish_impl(body)

    return router, next_impl, finish_impl, schedule_replay
