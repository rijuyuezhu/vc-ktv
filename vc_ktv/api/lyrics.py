"""Guest lyric search, selection, upload and provider-timeline validation."""

import asyncio
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Cookie, File, UploadFile
from starlette.concurrency import run_in_threadpool

from vc_ktv.application.errors import bail
from vc_ktv.application.guest_lyrics import LyricSelectionService
from vc_ktv.application.schemas import GuestLyricChoice
from vc_ktv.core.titles import title_key
from vc_ktv.infra.upload_limits import MAX_LRC_BYTES, read_lyric_upload
from vc_ktv.lyrics.lyric_selection import search_lyric_candidates
from vc_ktv.lyrics.lyric_sources import vcpedia_lyric_candidates


async def _netease_exact_lyrics(app, song: dict) -> list[dict]:
    """Use the selected song ID, with the title search as fallback."""
    try:
        return await app.state.netease_lyrics(
            int(song["source_id"]), title=song["title"]
        )
    except (httpx.HTTPError, ValueError, TypeError):
        return []


def create_lyrics_router(*, app, lyrics: LyricSelectionService):
    router = APIRouter()
    auth = lyrics.auth

    @router.get("/api/lyrics/search")
    async def lyrics_search(query: str, vc_guest: str | None = Cookie(default=None)):
        await run_in_threadpool(auth.require_user, vc_guest)
        return {"candidates": await app.state.lyrics_lookup(query)}

    @router.get("/api/admin/lyrics/vcpedia")
    async def fetch_vcpedia_lyrics(
        title: str, vc_admin: str | None = Cookie(default=None)
    ):
        await run_in_threadpool(auth.require_admin, vc_admin)
        try:
            variants = await vcpedia_lyric_candidates(title)
        except (httpx.HTTPError, ValueError) as error:
            bail(
                502 if isinstance(error, httpx.HTTPError) else 400,
                "无法获取歌词候选，请检查条目名称或稍后重试",
            )
        return {"candidates": variants}

    @router.post("/api/songs/{song_id}/lyrics/skip")
    def guest_skip_lyrics(song_id: int, vc_guest: str | None = Cookie(default=None)):
        return lyrics.skip(song_id, vc_guest)

    @router.get("/api/songs/{song_id}/lyrics/candidates")
    async def guest_lyric_candidates(
        song_id: int,
        query: str = "",
        phase: Literal["all", "vcpedia", "wiki", "lrclib", "netease", "more"] = "all",
        vc_guest: str | None = Cookie(default=None),
    ):
        song = await run_in_threadpool(lyrics.own_song, song_id, vc_guest)
        if song["status"] not in ("waiting", "ready"):
            bail(409, "这首歌已经开始播放或结束")
        term = query.strip() if query else title_key(song["title"]).strip()
        if not term:
            bail(400, "请输入歌名关键词")
        lyrics.check_search_allowed(song, song_id)
        lookup = (
            app.state.candidate_search(term)
            if phase == "all"
            else search_lyric_candidates(term, phase)
        )
        if song["source_type"] == "netease" and phase in ("all", "netease"):
            candidates, exact = await asyncio.gather(
                lookup, _netease_exact_lyrics(app, song)
            )
            # Exact-ID lyrics first; other source/version choices remain available.
            candidates = exact + [
                item
                for item in candidates
                if not exact
                or (item.get("text"), item.get("source"))
                != (exact[0]["text"], exact[0]["source"])
            ]
        else:
            candidates = await lookup
        lyrics.sign_candidates(song_id, candidates)
        return {"query": term, "candidates": candidates}

    @router.post("/api/songs/{song_id}/lyrics/select")
    def guest_select_lyrics(
        song_id: int,
        body: GuestLyricChoice,
        vc_guest: str | None = Cookie(default=None),
    ):
        return lyrics.select(song_id, body, vc_guest)

    @router.post("/api/songs/{song_id}/lyrics/upload")
    async def guest_upload_lrc(
        song_id: int,
        file: Annotated[UploadFile, File()],
        vc_guest: str | None = Cookie(default=None),
    ):
        song = await run_in_threadpool(lyrics.own_song, song_id, vc_guest)
        if not lyrics.gpu_alignment:
            bail(409, "CPU 模式暂不支持上传歌词重新对齐，请选择来源逐句时间轴")
        if not (file.filename or "").lower().endswith(".lrc"):
            bail(400, "请上传 .lrc 歌词文件")
        try:
            raw = await read_lyric_upload(file, MAX_LRC_BYTES, "LRC")
        finally:
            await file.close()
        # Uploaded timing is untrusted; the quality pipeline realigns text.
        text = await run_in_threadpool(lyrics.parse_lrc, raw)
        return await run_in_threadpool(
            lyrics.save_guest_lyrics, song_id, song["requester"], text, "上传 LRC"
        )

    return router
