"""Guest lyric search, selection, upload and provider-timeline validation."""

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Cookie, File, UploadFile

from vc_ktv.application.common import LRC_LINE, LRC_WORD_TIME, bail
from vc_ktv.application.schemas import GuestLyricChoice
from vc_ktv.core.store import time_now
from vc_ktv.core.titles import title_key
from vc_ktv.lyrics.lyric_selection import search_lyric_candidates
from vc_ktv.lyrics.lyric_sources import vcpedia_lyric_candidates
from vc_ktv.lyrics.timed_lyrics import (
    offset_timeline,
    project_provider_timeline,
    provider_timeline,
)


def create_lyrics_router(
    *,
    app,
    store,
    events,
    require_user,
    require_admin,
    gpu_alignment,
    sign,
    read_signed,
    lyric_search_times,
):
    router = APIRouter()

    @router.get("/api/lyrics/search")
    async def lyrics_search(query: str, vc_guest: str | None = Cookie(default=None)):
        require_user(vc_guest)
        return {"candidates": await app.state.lyrics_lookup(query)}

    @router.get("/api/admin/lyrics/vcpedia")
    async def fetch_vcpedia_lyrics(
        title: str, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        try:
            variants = await vcpedia_lyric_candidates(title)
        except (httpx.HTTPError, ValueError) as error:
            bail(
                502 if isinstance(error, httpx.HTTPError) else 400,
                "无法获取歌词候选，请检查条目名称或稍后重试",
            )
        return {"candidates": variants}

    def own_song(song_id: int, guest_token: str | None):
        guest = require_user(guest_token)["id"]
        with store.lock:
            song = store.db.execute(
                "SELECT * FROM songs WHERE id=? AND requester=?", (song_id, guest)
            ).fetchone()
        if not song:
            bail(404, "找不到你提交的歌曲")
        return dict(song)

    def save_guest_lyrics(
        song_id: int,
        requester: str,
        lyrics: str,
        source: str,
        *,
        timing_mode="align",
        provider_lrc="",
        source_lrc_raw="",
        offset_ms=0,
    ):
        if timing_mode != "provider" and not gpu_alignment:
            bail(409, "当前未启用 GPU 对齐，请选用来源逐句时间轴或不加歌词")
        with store.lock:
            current = store.db.execute(
                "SELECT * FROM songs WHERE id=? AND requester=?",
                (song_id, requester),
            ).fetchone()
            if not current or current["status"] not in ("waiting", "ready"):
                bail(409, "歌曲已经开始播放或结束，不能切换歌词")
            if current["prep_state"] == "aligning":
                bail(409, "字幕正在制作中，暂不能更换歌词")
            next_state = (
                "downloaded" if current["status"] == "ready" else current["prep_state"]
            )
            try:
                store.db.execute(
                    "UPDATE songs SET lyric_choice=?,lyric_source=?,prep_state=?,"
                    "lyric_timing_mode=?,provider_lrc=?,source_lrc_raw=?,lyric_offset_ms=?,"
                    "skip_subtitles=0,prep_stage='queued',prep_error='' WHERE id=?",
                    (
                        lyrics,
                        source,
                        next_state,
                        timing_mode,
                        provider_lrc,
                        source_lrc_raw,
                        offset_ms,
                        song_id,
                    ),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "status": next_state}

    @router.post("/api/songs/{song_id}/lyrics/skip")
    def guest_skip_lyrics(song_id: int, vc_guest: str | None = Cookie(default=None)):
        song = own_song(song_id, vc_guest)
        with store.lock:
            current = store.db.execute(
                "SELECT status,prep_state,prep_stage,source_file FROM songs WHERE id=? AND requester=?",
                (song_id, song["requester"]),
            ).fetchone()
            if not current or current["status"] not in ("waiting", "ready"):
                bail(409, "只能对尚未播放的歌曲选择原片")
            ready = current["status"] == "ready"
            if (
                ready
                and not (
                    store.media / Path(current["source_file"] or "").name
                ).is_file()
            ):
                bail(409, "原视频尚未准备好")
            try:
                store.db.execute(
                    "UPDATE songs SET skip_subtitles=1,lyric_choice='',lyric_source='',"
                    "lyric_timing_mode='align',provider_lrc='',source_lrc_raw='',lyric_offset_ms=0,"
                    "media_file=CASE WHEN ? THEN source_file ELSE media_file END,"
                    "prep_state=?,prep_stage=?,prep_stage_at=?,prep_error='' WHERE id=?",
                    (
                        ready,
                        "original" if ready else current["prep_state"],
                        "skipped" if ready else current["prep_stage"],
                        time_now(),
                        song_id,
                    ),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "source": "original"}

    @router.get("/api/songs/{song_id}/lyrics/candidates")
    async def guest_lyric_candidates(
        song_id: int,
        query: str = "",
        phase: Literal["all", "vcpedia", "wiki", "lrclib", "netease", "more"] = "all",
        vc_guest: str | None = Cookie(default=None),
    ):
        song = own_song(song_id, vc_guest)
        if song["status"] not in ("waiting", "ready"):
            bail(409, "这首歌已经开始播放或结束")
        term = query.strip() if query else title_key(song["title"]).strip()
        if not term:
            bail(400, "请输入歌名关键词")
        key = (song["requester"], song_id)
        recent = [t for t in lyric_search_times.get(key, []) if t > time_now() - 300]
        if len(recent) >= 30:
            bail(429, "搜索太频繁，请五分钟后重试")
        recent.append(time_now())
        lyric_search_times[key] = recent
        lookup = (
            app.state.candidate_search(term)
            if phase == "all"
            else search_lyric_candidates(term, phase)
        )
        if song["source_type"] == "netease" and phase in ("all", "netease"):

            async def exact_by_id():
                try:
                    return await app.state.netease_lyrics(
                        int(song["source_id"]), title=song["title"]
                    )
                except (httpx.HTTPError, ValueError, TypeError):
                    return []

            candidates, exact = await asyncio.gather(lookup, exact_by_id())
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
        for candidate in candidates:
            digest = hashlib.sha256(candidate["text"].encode()).hexdigest()
            candidate["ticket"] = sign(
                "lyric",
                f"{events.active_id}:{song_id}:{candidate['source']}:{digest}",
                30 * 60,
            )
            if original := candidate.get("unfiltered_text"):
                original_digest = hashlib.sha256(original.encode()).hexdigest()
                candidate["unfiltered_ticket"] = sign(
                    "lyric",
                    f"{events.active_id}:{song_id}:{candidate['source']}:{original_digest}",
                    30 * 60,
                )
            if timed := candidate.get("timed_lrc"):
                raw = candidate["source_lrc_raw"]
                payload = hashlib.sha256(
                    (candidate["text"] + "\0" + timed + "\0" + raw).encode()
                ).hexdigest()
                candidate["timing_ticket"] = sign(
                    "lyrictime",
                    f"{events.active_id}:{song_id}:{candidate['source']}:{payload}",
                    30 * 60,
                )
        return {"query": term, "candidates": candidates}

    @router.post("/api/songs/{song_id}/lyrics/select")
    def guest_select_lyrics(
        song_id: int,
        body: GuestLyricChoice,
        vc_guest: str | None = Cookie(default=None),
    ):
        song = own_song(song_id, vc_guest)
        digest = hashlib.sha256(body.text.encode()).hexdigest()
        if (
            read_signed(body.ticket, "lyric")
            != f"{events.active_id}:{song_id}:{body.source}:{digest}"
        ):
            bail(403, "歌词候选已失效，请重新搜索")
        if body.source not in ("VCPedia", "LRCLIB", "网易云音乐", "UtaTen", "J-Lyric"):
            bail(400, "歌词来源不受支持")
        if len(body.text.splitlines()) < 2:
            bail(400, "歌词候选无效")
        chosen = body.text
        if body.selected_lines is not None:
            if body.source != "网易云音乐":
                bail(400, "逐行校正仅支持网易云音乐")
            original_digest = hashlib.sha256(body.selection_text.encode()).hexdigest()
            if read_signed(body.selection_ticket, "lyric") != (
                f"{events.active_id}:{song_id}:{body.source}:{original_digest}"
            ):
                bail(403, "校正歌词已失效，请重新搜索")
            lines = body.selection_text.splitlines()
            selected = body.selected_lines
            if (
                len(selected) < 2
                or any(not 0 <= index < len(lines) for index in selected)
                or selected != sorted(set(selected))
            ):
                bail(400, "请至少保留两行，且不能调整歌词顺序")
            chosen = "\n".join(lines[index] for index in selected)
        if body.timing_mode == "provider":
            payload = hashlib.sha256(
                (
                    body.text + "\0" + body.timed_lrc + "\0" + body.source_lrc_raw
                ).encode()
            ).hexdigest()
            if read_signed(body.timing_ticket, "lyrictime") != (
                f"{events.active_id}:{song_id}:{body.source}:{payload}"
            ):
                bail(403, "来源时间轴已失效，请重新选择歌词")
            if provider_timeline(body.source_lrc_raw, body.text) != body.timed_lrc:
                bail(400, "来源时间轴与已选歌词不匹配")
            timed = body.timed_lrc
            try:
                if body.selected_lines is not None:
                    timed = project_provider_timeline(
                        body.source_lrc_raw, body.selection_text, body.selected_lines
                    )
                offset_timeline(timed, body.offset_ms, song["duration"])
            except ValueError as error:
                bail(400, str(error))
            return save_guest_lyrics(
                song_id,
                song["requester"],
                chosen,
                body.source,
                timing_mode="provider",
                provider_lrc=timed,
                source_lrc_raw=body.source_lrc_raw,
                offset_ms=body.offset_ms,
            )
        return save_guest_lyrics(song_id, song["requester"], chosen, body.source)

    @router.post("/api/songs/{song_id}/lyrics/upload")
    async def guest_upload_lrc(
        song_id: int,
        file: Annotated[UploadFile, File()],
        vc_guest: str | None = Cookie(default=None),
    ):
        song = own_song(song_id, vc_guest)
        if not gpu_alignment:
            bail(409, "CPU 模式暂不支持上传歌词重新对齐，请选择来源逐句时间轴")
        if not (file.filename or "").lower().endswith(".lrc"):
            bail(400, "请上传 .lrc 歌词文件")
        raw = await file.read()
        await file.close()
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            bail(400, "LRC 文件需要使用 UTF-8 编码")
        lines = []
        for line in content.splitlines():
            match = LRC_LINE.match(line.strip())
            if match:
                lyric = LRC_WORD_TIME.sub("", match.group(1)).strip()
                if lyric:
                    lines.append(lyric)
        lyrics = "\n".join(lines)
        if len(lines) < 2 or len(lyrics) < 10:
            bail(400, "LRC 需要至少两行带时间标签的有效歌词")
        # Uploaded timing is not trusted; the original quality pipeline realigns the text.
        return save_guest_lyrics(song_id, song["requester"], lyrics, "上传 LRC")

    return router
