"""Signed playback media and HTML subtitle endpoints."""

import json
from pathlib import Path

from fastapi import APIRouter

from vc_ktv.application.common import MEDIA_MIMES, bail


def create_media_router(*, media, auth):
    router = APIRouter()
    store = media.store
    events = media.events
    read_signed = auth.read_signed

    @router.get("/media/{song_id}/source")
    def serve_source(song_id: int, token: str = ""):
        if (
            not events.active_id
            or read_signed(token, "media") != f"{events.active_id}:{song_id}"
        ):
            bail(403, "媒体链接无效或已过期")
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "原视频不存在")
            song = store.replay_assets(row)
            if not song.get("source_file"):
                bail(404, "原视频不存在")
            path = media.overlay_source(song)
            if path is None:
                bail(409, "原片模式不可用，请使用烧录版")
            if not path.is_file():
                bail(404, "原视频文件缺失")
            return media.pinned_response(
                path,
                media_type=MEDIA_MIMES.get(path.suffix, "video/mp4"),
                content_disposition_type="inline",
            )

    @router.get("/api/tv/subtitles/{song_id}")
    def tv_subtitles(song_id: int, token: str):
        if (
            not events.active_id
            or read_signed(token, "overlay") != f"{events.active_id}:{song_id}"
        ):
            bail(403, "字幕链接无效或已过期")
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            folder = media.overlay_bundle(dict(row))
        if folder is None:
            bail(404, "当前歌曲没有可用的逐字字幕")
        try:
            return {
                "timeline": json.loads(
                    (folder / "timeline.json").read_text(encoding="utf-8")
                ),
                "plan": json.loads(
                    (folder / "display_plan.json").read_text(encoding="utf-8")
                ),
                "style": json.loads(
                    (folder / "style.json").read_text(encoding="utf-8")
                ),
            }
        except (OSError, ValueError):
            bail(404, "字幕数据不可用")

    @router.get("/media/{song_id}")
    def serve_media(song_id: int, token: str = ""):
        if (
            read_signed(token, "media") != f"{events.active_id}:{song_id}"
            or not events.active_id
        ):
            bail(403, "媒体链接无效或已过期")
        # Share AutoPrep's Store mutex: do not serve an uncommitted media path.
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "媒体不存在")
            asset = store.replay_assets(row)
            if not asset.get("media_file"):
                bail(404, "媒体不存在")
            name = Path(asset["media_file"]).name
            file = store.media / name
            if not file.is_file():
                bail(404, "媒体文件缺失")
            return media.pinned_response(
                file,
                media_type=MEDIA_MIMES.get(file.suffix, "application/octet-stream"),
                filename=name,
                content_disposition_type="inline",
            )

    return router
