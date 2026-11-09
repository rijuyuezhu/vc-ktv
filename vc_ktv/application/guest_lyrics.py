"""Guest lyric selection, time-axis capabilities and atomic song changes."""

import hashlib
import sqlite3
from pathlib import Path

from vc_ktv.application.common import LRC_LINE, LRC_WORD_TIME, bail
from vc_ktv.application.schemas import GuestLyricChoice
from vc_ktv.core.store import time_now
from vc_ktv.lyrics.timed_lyrics import (
    offset_timeline,
    project_provider_timeline,
    provider_timeline,
)


class LyricSelectionService:
    def __init__(self, store, events, auth, gpu_alignment: bool):
        self.store = store
        self.events = events
        self.auth = auth
        self.gpu_alignment = gpu_alignment
        self.search_times: dict[tuple[str, int], list[int]] = {}

    def own_song(self, song_id: int, guest_token: str | None) -> dict:
        guest = self.auth.require_user(guest_token)["id"]
        with self.store.lock:
            song = self.store.db.execute(
                "SELECT * FROM songs WHERE id=? AND requester=?", (song_id, guest)
            ).fetchone()
        if not song:
            bail(404, "找不到你提交的歌曲")
        return dict(song)

    def save_guest_lyrics(
        self,
        song_id: int,
        requester: str,
        lyrics: str,
        source: str,
        *,
        timing_mode: str = "align",
        provider_lrc: str = "",
        source_lrc_raw: str = "",
        offset_ms: int = 0,
    ) -> dict:
        if timing_mode != "provider" and not self.gpu_alignment:
            bail(409, "当前未启用 GPU 对齐，请选用来源逐句时间轴或不加歌词")
        with self.store.lock:
            current = self.store.db.execute(
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
                self.store.db.execute(
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
                self.store.db.commit()
            except sqlite3.Error:
                self.store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "status": next_state}

    def skip(self, song_id: int, guest_token: str | None) -> dict:
        song = self.own_song(song_id, guest_token)
        with self.store.lock:
            current = self.store.db.execute(
                "SELECT status,prep_state,prep_stage,source_file FROM songs WHERE id=? AND requester=?",
                (song_id, song["requester"]),
            ).fetchone()
            if not current or current["status"] not in ("waiting", "ready"):
                bail(409, "只能对尚未播放的歌曲选择原片")
            ready = current["status"] == "ready"
            if (
                ready
                and not (
                    self.store.media / Path(current["source_file"] or "").name
                ).is_file()
            ):
                bail(409, "原视频尚未准备好")
            try:
                self.store.db.execute(
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
                self.store.db.commit()
            except sqlite3.Error:
                self.store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "source": "original"}

    def select(
        self, song_id: int, body: GuestLyricChoice, guest_token: str | None
    ) -> dict:
        song = self.own_song(song_id, guest_token)
        digest = hashlib.sha256(body.text.encode()).hexdigest()
        if (
            self.auth.read_signed(body.ticket, "lyric")
            != f"{self.events.active_id}:{song_id}:{body.source}:{digest}"
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
            if self.auth.read_signed(body.selection_ticket, "lyric") != (
                f"{self.events.active_id}:{song_id}:{body.source}:{original_digest}"
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
            if self.auth.read_signed(body.timing_ticket, "lyrictime") != (
                f"{self.events.active_id}:{song_id}:{body.source}:{payload}"
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
            return self.save_guest_lyrics(
                song_id,
                song["requester"],
                chosen,
                body.source,
                timing_mode="provider",
                provider_lrc=timed,
                source_lrc_raw=body.source_lrc_raw,
                offset_ms=body.offset_ms,
            )
        return self.save_guest_lyrics(song_id, song["requester"], chosen, body.source)

    def check_search_allowed(self, song: dict, song_id: int) -> None:
        key = (song["requester"], song_id)
        recent = [t for t in self.search_times.get(key, []) if t > time_now() - 300]
        if len(recent) >= 30:
            bail(429, "搜索太频繁，请五分钟后重试")
        recent.append(time_now())
        self.search_times[key] = recent

    def sign_candidates(self, song_id: int, candidates: list[dict]) -> None:
        for candidate in candidates:
            digest = hashlib.sha256(candidate["text"].encode()).hexdigest()
            candidate["ticket"] = self.auth.sign(
                "lyric",
                f"{self.events.active_id}:{song_id}:{candidate['source']}:{digest}",
                30 * 60,
            )
            if original := candidate.get("unfiltered_text"):
                original_digest = hashlib.sha256(original.encode()).hexdigest()
                candidate["unfiltered_ticket"] = self.auth.sign(
                    "lyric",
                    f"{self.events.active_id}:{song_id}:{candidate['source']}:{original_digest}",
                    30 * 60,
                )
            if timed := candidate.get("timed_lrc"):
                raw = candidate["source_lrc_raw"]
                payload = hashlib.sha256(
                    (candidate["text"] + "\0" + timed + "\0" + raw).encode()
                ).hexdigest()
                candidate["timing_ticket"] = self.auth.sign(
                    "lyrictime",
                    f"{self.events.active_id}:{song_id}:{candidate['source']}:{payload}",
                    30 * 60,
                )

    @staticmethod
    def parse_lrc(raw: bytes) -> str:
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
        return lyrics
