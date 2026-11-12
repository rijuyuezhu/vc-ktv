"""Read-only song history and per-song detail endpoints."""

import json
from pathlib import Path

from fastapi import APIRouter, Cookie

from vc_ktv.application.errors import bail
from vc_ktv.core.store import Store
from vc_ktv.core.titles import title_key


def create_history_router(
    app,
    *,
    current_user,
    admin_principal,
    require_user,
) -> APIRouter:
    router = APIRouter()
    store = app.state.store
    events = app.state.events

    @router.get("/api/songs/history")
    def song_history(
        admin_view: bool = False,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        user = current_user(vc_guest)
        principal = admin_principal(vc_admin)
        if admin_view and not principal:
            bail(401, "需要管理员登录")
        if not user and not principal:
            require_user(vc_guest)
        if not events.active_id:
            return {"songs": [], "archived": False}
        with store.lock:
            order = store.close_song_order()
            records = store.rows(
                """SELECT s.id,s.bvid,s.page,s.title,s.uploader,s.status,
                          s.source_type,s.source_id,s.prep_state,
                          s.skip_subtitles,s.media_file,s.replay_of,
                          g.name AS nickname
                   FROM songs s JOIN guests g ON g.id=s.requester"""
            )
            rank = {song_id: i for i, song_id in enumerate(order)}
            records.sort(key=lambda song: rank.get(song["id"], len(rank)))
            for song in records:
                asset = store.replay_assets(song)
                song["downloadable"] = (
                    Store.downloadable(asset)
                    and (
                        store.media / Path(asset.get("media_file") or "").name
                    ).is_file()
                )
                song.pop("media_file", None)
            return {"songs": records, "archived": False}

    @router.get("/api/songs/played")
    def guest_played_songs(
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if admin_principal(vc_admin) is None:
            require_user(vc_guest)
        with store.lock:
            rows = store.rows(
                """SELECT s.id,s.bvid,s.source_type,s.source_id,s.title,s.duration,
                          s.uploader,s.played_at,s.page,s.prep_state,s.media_file,s.replay_of,s.media_file IS NOT NULL AS has_media,
                          g.name AS nickname
                   FROM songs s JOIN guests g ON g.id=s.requester
                   WHERE s.status='played'
                   ORDER BY s.played_at DESC,s.id DESC"""
            )
            for song in rows:
                asset = store.replay_assets(song)
                song["downloadable"] = (
                    Store.downloadable({**asset, "status": "played"})
                    and (
                        store.media / Path(asset.get("media_file") or "").name
                    ).is_file()
                )
                song.pop("media_file", None)
            return {"songs": rows}

    @router.get("/api/songs/{song_id}/detail")
    def song_details(
        song_id: int,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        guest = current_user(vc_guest)
        if guest is None and admin_principal(vc_admin) is None:
            require_user(vc_guest)
        with store.lock:
            row = store.db.execute(
                """SELECT s.id,s.title,s.bvid,s.page,s.source_type,s.source_id,
                          s.uploader,s.cover,s.duration,s.status,s.prep_state,s.prep_error,s.prep_stage,
                          s.prep_stage_at,s.prep_timing,s.prep_attempts,s.prep_retry_at,
                          s.skip_subtitles,s.priority_next,
                          s.played_at,s.media_file,s.replay_of,s.media_file IS NOT NULL AS has_media,
                          s.lyric_choice!='' AS lyric_selected,
                          s.lyric_timing_mode,s.lyric_offset_ms,s.source_file,
                          g.id AS requester_id,g.name AS nickname
                   FROM songs s JOIN guests g ON g.id=s.requester
                   WHERE s.id=?""",
                (song_id,),
            ).fetchone()
            if row is None:
                bail(404, "歌曲不存在")
            song = store.replay_assets(row)
            song["has_media"] = bool(song.get("media_file"))
            try:
                song["stage_seconds"] = json.loads(song.pop("prep_timing"))
            except (ValueError, TypeError):
                song["stage_seconds"] = {}
            song["mine"] = bool(guest and guest["id"] == song["requester_id"])
            song["downloadable"] = (
                Store.downloadable(song)
                and (store.media / Path(song["media_file"] or "").name).is_file()
            )
            source = store.media / Path(song["source_file"] or "").name
            song["source_downloadable"] = bool(
                song["source_file"]
                and source.is_file()
                and song["status"] in ("ready", "playing", "played", "skipped")
            )
            song.pop("source_file", None)
            song.pop("media_file", None)
            song["playable"] = Store.playable(song, store.settings())
            song["lyric_query"] = title_key(song["title"])
            return song

    return router
