"""Authenticated song media downloads, including replay assets."""

import re
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Cookie

from vc_ktv.application.errors import bail
from vc_ktv.application.media import MEDIA_MIMES
from vc_ktv.core.artifacts import download_render
from vc_ktv.core.store import Store
from vc_ktv.core.titles import title_key


def create_download_router(*, store, events, admin_principal, require_user, media):
    router = APIRouter()

    def require_downloads_allowed():
        if store.tv_online():
            bail(409, "主屏在线，暂不可下载或生成成片")

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
            require_downloads_allowed()
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            asset = store.replay_assets(row)
            if Store.downloadable(asset):
                file = store.media / Path(asset["media_file"]).name
            else:
                _, _, file = download_render(store, asset)
                if file is None:
                    bail(409, "视频尚未准备好")
            if not file.is_file():
                bail(404, "媒体文件缺失")
            title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title_key(row["title"]))
            title = title.strip(" ._")[:48] or "video"
            return media.pinned_response(
                file,
                media_type=MEDIA_MIMES.get(file.suffix, "application/octet-stream"),
                filename=f"vc-ktv-{song_id}-{title}{file.suffix}",
                content_disposition_type="attachment",
                headers={"Cache-Control": "private, no-store"},
            )

    @router.post("/api/songs/{song_id}/download/request")
    def request_download(
        song_id: int,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if not events.active_id:
            bail(404, "当前没有活动场次")
        if admin_principal(vc_admin) is None:
            require_user(vc_guest)
        with store.lock:
            require_downloads_allowed()
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            asset = store.replay_assets(row)
            if (
                Store.downloadable(asset)
                and (store.media / Path(asset["media_file"]).name).is_file()
            ):
                return {"status": "ready"}
            status, attempt, _ = download_render(store, asset)
            if attempt is None:
                bail(409, "字幕尚未准备好")
            if status in ("available", "failed"):
                try:
                    store.db.execute(
                        "INSERT INTO download_renders(attempt,status,requested_at) VALUES(?,'queued',?) "
                        "ON CONFLICT(attempt) DO UPDATE SET status='queued',requested_at=excluded.requested_at",
                        (attempt, int(time.time())),
                    )
                    store.db.commit()
                except sqlite3.Error:
                    store.db.rollback()
                    raise
                status = "queued"
            return {"status": status}

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
            require_downloads_allowed()
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
            return media.pinned_response(
                file,
                media_type=MEDIA_MIMES.get(file.suffix, "application/octet-stream"),
                filename=f"vc-ktv-{song_id}-{title}-original{file.suffix}",
                content_disposition_type="attachment",
                headers={"Cache-Control": "private, no-store"},
            )

    return router
