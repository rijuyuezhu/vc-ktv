"""Administrator preparation routes: authentication and HTTP transport."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Cookie, File, UploadFile
from starlette.concurrency import run_in_threadpool

from vc_ktv.application.errors import bail
from vc_ktv.application.media import MEDIA_MIMES
from vc_ktv.application.schemas import Lyrics, RenderRequest
from vc_ktv.infra.upload_limits import MAX_TEXTGRID_BYTES, read_lyric_upload


def create_preparation_admin_router(*, preparation, require_admin, manual):
    router = APIRouter()

    @router.post("/api/admin/songs/{song_id}/tv-cache/retry")
    def retry_tv_cache(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return preparation.retry_tv_cache(song_id)

    @router.post("/api/admin/songs/{song_id}/subtitles/retry")
    def retry_subtitles(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return preparation.retry_subtitles(song_id)

    @router.post("/api/admin/songs/{song_id}/download/retry")
    def retry_media_download(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return preparation.retry_media_download(song_id)

    @router.post("/api/admin/songs/{song_id}/subtitles/skip")
    def skip_subtitles(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return preparation.skip_subtitles(song_id)

    @router.post("/api/admin/songs/{song_id}/media")
    async def upload(
        song_id: int,
        file: Annotated[UploadFile, File()],
        vc_admin: str | None = Cookie(default=None),
    ):
        require_admin(vc_admin)
        try:
            suffix = Path(file.filename or "").suffix.lower()
            if suffix not in MEDIA_MIMES:
                bail(400, "仅支持 MP4、M4V、WebM 文件")
            # The whole disk/probe/publish lifecycle is synchronous. Keep it in
            # one worker-thread operation, not partly on the asyncio loop.
            return await run_in_threadpool(
                preparation.upload_media, song_id, suffix, file.file.read
            )
        finally:
            await file.close()

    @router.post("/api/admin/songs/{song_id}/lyrics")
    def set_lyrics(
        song_id: int, body: Lyrics, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        return preparation.set_lyrics(song_id, body.text)

    @router.get("/api/admin/songs/{song_id}/lyrics")
    def get_lyrics(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return preparation.get_lyrics(song_id)

    @router.post("/api/admin/songs/{song_id}/alignment/import")
    async def import_textgrid(
        song_id: int,
        file: Annotated[UploadFile, File()],
        offset: float = 0,
        vc_admin: str | None = Cookie(default=None),
    ):
        require_admin(vc_admin)
        try:
            if not (file.filename or "").lower().endswith(".textgrid"):
                bail(400, "请上传 TIFA TextGrid 文件")
            content = await read_lyric_upload(file, MAX_TEXTGRID_BYTES, "TextGrid")
        finally:
            await file.close()
        return await run_in_threadpool(
            preparation.import_textgrid, song_id, content, offset
        )

    @router.post("/api/admin/songs/{song_id}/render")
    def render_song(
        song_id: int, body: RenderRequest, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        return manual.start(song_id, body.lead)

    return router
