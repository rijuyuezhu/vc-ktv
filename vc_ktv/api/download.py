"""Authenticated song media downloads, including replay assets."""

import re
from pathlib import Path

from fastapi import APIRouter, Cookie

from vc_ktv.application.common import bail
from vc_ktv.core.store import Store
from vc_ktv.core.titles import title_key

MEDIA_MIMES = {".mp4": "video/mp4", ".webm": "video/webm", ".m4v": "video/mp4"}


def create_download_router(
    *, store, events, admin_principal, require_user, pinned_media_response
):
    router = APIRouter()

    @router.get("/api/songs/{song_id}/download")
    def download_song(
        song_id: int,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if not events.active_id:
            bail(404, "当前没有活动场次")
        if admin_principal(vc_admin) is None:
            require_user(vc_guest)
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            asset = store.replay_assets(row)
            if not Store.downloadable(asset):
                bail(409, "视频尚未准备好")
            file = store.media / Path(asset["media_file"]).name
            if not file.is_file():
                bail(404, "媒体文件缺失")
            title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title_key(row["title"]))
            title = title.strip(" ._")[:48] or "video"
            return pinned_media_response(
                file,
                media_type=MEDIA_MIMES.get(file.suffix, "application/octet-stream"),
                filename=f"vc-ktv-{song_id}-{title}{file.suffix}",
                content_disposition_type="attachment",
                headers={"Cache-Control": "private, no-store"},
            )

    @router.get("/api/songs/{song_id}/download/source")
    def download_original(
        song_id: int,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if not events.active_id:
            bail(404, "当前没有活动场次")
        if admin_principal(vc_admin) is None:
            require_user(vc_guest)
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            asset = store.replay_assets(row)
            if row["status"] not in (
                "ready",
                "playing",
                "played",
                "skipped",
            ) or not asset.get("source_file"):
                bail(409, "原片尚未准备好")
            file = store.media / Path(asset["source_file"]).name
            if not file.is_file():
                bail(404, "原片文件缺失")
            title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title_key(row["title"]))
            title = title.strip(" ._")[:48] or "video"
            return pinned_media_response(
                file,
                media_type=MEDIA_MIMES.get(file.suffix, "application/octet-stream"),
                filename=f"vc-ktv-{song_id}-{title}-original{file.suffix}",
                content_disposition_type="attachment",
                headers={"Cache-Control": "private, no-store"},
            )

    return router
