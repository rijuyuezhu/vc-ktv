"""Self-hosted BV-first karaoke with optional local subtitle generation."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from fastapi import Cookie, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .accounts import password_hash, verify_password
from .autoprep import AutoPrep, title_key
from .disk_budget import DiskBudgetExceeded, EventDiskBudget
from .events import CurrentStore, Events
from .karaoke import render_video, write_ass
from .lyric_selection import netease_song_lyrics, search_lyric_candidates
from .lyric_sources import vcpedia_lyric_candidates
from .media_pins import MediaPins
from .netease_catalog import lookup_netease_song
from .netease_discovery import resolve_song_reference, search_netease_songs
from .netease_login import NeteaseLogin, NeteaseLoginError
from .process_cancel import terminate_thread_children
from .subtitle_styles import StylePreset
from .textgrid_import import convert_textgrid
from .timed_lyrics import offset_timeline, project_provider_timeline, provider_timeline

BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")
MAX_ACTIVE_SONGS = 1000
MAX_USER_SONGS = 10
LRC_LINE = re.compile(
    r"^(?:\[(?:(?:\d{1,2}:)?\d{1,3}:\d{2}(?:[.:]\d{1,3})?)\])+\s*(.*)$"
)
LRC_WORD_TIME = re.compile(r"<(?:(?:\d{1,2}:)?\d{1,3}:\d{2}(?:[.:]\d{1,3})?)>")
STATIC = Path(__file__).resolve().parent.parent / "static"
MEDIA_MIMES = {".mp4": "video/mp4", ".webm": "video/webm", ".m4v": "video/mp4"}


def time_now():
    return int(time.time())


def bail(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


def get_bvid(raw: str) -> str:
    raw = raw.strip()
    # Accept either an exact BV ID or common Bilibili video URLs.
    match = re.search(r"(?:^|/)(BV[0-9A-Za-z]{10})(?:[/?#]|$)", raw)
    v = match.group(1) if match else raw
    if not BVID.fullmatch(v):
        bail(400, "请输入 BV 号或含 BV 号的视频链接")
    return v


class TVPair(BaseModel):
    pair: str


class Preview(BaseModel):
    bvid: str
    page: int = Field(default=1, ge=1)


class RequestSong(Preview):
    nickname: str = Field(default="", max_length=24)


class RequestNetEaseSong(BaseModel):
    ref: str = Field(min_length=1, max_length=2048)


class ImportNetEaseCookie(BaseModel):
    music_u: str = Field(min_length=1, max_length=4104)


class GuestLyricChoice(BaseModel):
    text: str = Field(min_length=10)
    ticket: str = Field(min_length=10, max_length=256)
    source: str = Field(min_length=2, max_length=20)
    timing_mode: Literal["align", "provider"] = "align"
    timed_lrc: str = Field(default="")
    source_lrc_raw: str = Field(default="")
    timing_ticket: str = ""
    offset_ms: int = Field(default=0)
    selection_text: str = Field(default="")
    selection_ticket: str = ""
    selected_lines: list[int] | None = Field(default=None)


class Login(BaseModel):
    password: str
    username: str | None = None


class EventAdminCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=24)
    password: str = Field(min_length=6, max_length=72)


class EventAdminPassword(BaseModel):
    password: str = Field(min_length=6, max_length=72)


class PlaybackSettings(BaseModel):
    queue_policy: Literal["fair", "fcfs"] | None = None
    allow_unsubtitled: bool | None = None
    # Old clients that omit the new field must not reset the chosen preset.
    subtitle_style: StylePreset | None = None
    video_quality: Literal[720, 480] | None = None
    # Optional to preserve older admin clients' current per-event setting.
    max_bandwidth_mbps: float | None = Field(default=None, ge=1)
    orders_paused: bool | None = None
    tv_html_overlay: bool | None = None


class EventCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    code: str | None = None


class JoinEvent(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class RegisterUser(BaseModel):
    username: str = Field(min_length=1, max_length=24)
    password: str = Field(min_length=1, max_length=72)
    confirm_password: str = Field(min_length=1, max_length=72)


class UserLogin(BaseModel):
    username: str = Field(min_length=1, max_length=24)
    password: str = Field(min_length=1, max_length=72)


class Finish(BaseModel):
    song_id: int
    reason: str = ""


class PlaybackMode(BaseModel):
    mode: Literal["browser", "tv", "manual"]


class TVRemoteCommand(BaseModel):
    action: Literal[
        "pause",
        "resume",
        "restart",
        "skip",
        "replay_next",
        "volume",
        "offset",
        "opacity",
        "smooth",
    ]
    song_id: int | None = Field(default=None, ge=1)
    value: float | bool | None = None


class TVRemoteReport(BaseModel):
    ack_seq: int = Field(ge=0)
    active_id: int | None = None
    paused: bool = True
    position: float = Field(default=0, ge=0)
    duration: float | None = Field(default=None, ge=0)
    volume: int = Field(default=100, ge=0, le=100)
    offset: int = Field(default=0, ge=-600, le=600)
    opacity: int = Field(default=15, ge=0, le=100)
    smooth: bool = False
    started: bool = False
    overlay: bool = False
    error: str = Field(default="", max_length=150)


class Lyrics(BaseModel):
    text: str = Field()


class RenderRequest(BaseModel):
    lead: float = Field(default=1.8, ge=0, le=5)


def probe_media(file: Path) -> float:
    """Validate a real video file and obtain its actual duration."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(file),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        data = json.loads(proc.stdout)
        streams = data.get("streams", [])
        if not any(
            s.get("codec_type") == "video"
            and 0 < s.get("width", 0) <= 7680
            and 0 < s.get("height", 0) <= 4320
            for s in streams
        ):
            raise ValueError("未找到有效的视频轨道")
        duration = float(data["format"]["duration"])
        if duration <= 0:
            raise ValueError("视频时长必须在 1 秒至 1 小时之间")
        return duration
    except (
        subprocess.SubprocessError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError("视频无效或无法解析，请上传有效的 MP4 或 WebM") from error


class PinnedFileResponse(FileResponse):
    """Keep a media inode alive through FileResponse's deferred open/send."""

    async def __call__(self, scope, receive, send):
        # A pathsend event may be queued for later opening. Stream the file
        # here instead so the pinned link is safe to remove in finally.
        if "http.response.pathsend" in scope.get("extensions", {}):
            scope = {
                **scope,
                "extensions": {
                    key: value
                    for key, value in scope["extensions"].items()
                    if key != "http.response.pathsend"
                },
            }
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.media = root / "media"
        self.media.mkdir(exist_ok=True)
        self.disk_budget = EventDiskBudget(root)
        self.db = sqlite3.connect(root / "room.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS guests(
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                plays INTEGER NOT NULL DEFAULT 0,
                seconds INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY REFERENCES guests(id),
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS songs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT NOT NULL, page INTEGER NOT NULL,
                title TEXT NOT NULL, uploader TEXT NOT NULL,
                cover TEXT NOT NULL, duration INTEGER NOT NULL,
                requester TEXT NOT NULL REFERENCES guests(id),
                requested_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
                media_file TEXT, lyrics TEXT NOT NULL DEFAULT '',
                played_at INTEGER);
            CREATE TABLE IF NOT EXISTS room_state(
                name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS event_admins(
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1);
        """)
        # Scheduling offsets are deliberately separate from real performance
        # statistics: late arrivals must not appear to have sung before joining.
        guest_columns = {row[1] for row in self.db.execute("PRAGMA table_info(guests)")}
        # fair_seconds is a legacy database column retained for compatibility,
        # but is never read by the song-count-only scheduler.
        for name in ("fair_offset", "fair_seconds"):
            if name not in guest_columns:
                self.db.execute(
                    f"ALTER TABLE guests ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(songs)")}
        if "source_file" not in columns:
            self.db.execute("ALTER TABLE songs ADD COLUMN source_file TEXT")
            self.db.execute(
                "UPDATE songs SET source_file=media_file WHERE media_file IS NOT NULL"
            )
        if "render_error" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN render_error TEXT NOT NULL DEFAULT ''"
            )
        self.db.execute(
            "UPDATE songs SET status='ready',render_error='制作中断，可重新尝试' WHERE status='rendering' AND source_file IS NOT NULL"
        )
        if "prep_state" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN prep_state TEXT NOT NULL DEFAULT 'pending'"
            )
            self.db.execute(
                "UPDATE songs SET prep_state='manual' "
                "WHERE media_file IS NOT NULL OR status != 'waiting'"
            )
        if "prep_error" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN prep_error TEXT NOT NULL DEFAULT ''"
            )
        if "prep_attempts" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN prep_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "prep_retry_at" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN prep_retry_at INTEGER NOT NULL DEFAULT 0"
            )
        if "lyric_choice" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN lyric_choice TEXT NOT NULL DEFAULT ''"
            )
        if "lyric_source" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN lyric_source TEXT NOT NULL DEFAULT ''"
            )
        for column, ddl in (
            ("fair_exempt", "INTEGER NOT NULL DEFAULT 0"),
            ("skip_subtitles", "INTEGER NOT NULL DEFAULT 0"),
            ("prep_stage", "TEXT NOT NULL DEFAULT ''"),
            ("prep_stage_at", "INTEGER NOT NULL DEFAULT 0"),
            ("prep_timing", "TEXT NOT NULL DEFAULT '{}'"),
            ("priority_next", "INTEGER NOT NULL DEFAULT 0"),
            ("replay_of", "INTEGER"),
            ("lyric_timing_mode", "TEXT NOT NULL DEFAULT 'align'"),
            ("provider_lrc", "TEXT NOT NULL DEFAULT ''"),
            ("source_lrc_raw", "TEXT NOT NULL DEFAULT ''"),
            ("lyric_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("source_type", "TEXT NOT NULL DEFAULT 'bilibili'"),
            ("source_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                self.db.execute(f"ALTER TABLE songs ADD COLUMN {column} {ddl}")
        if "archive_order" not in columns:
            self.db.execute("ALTER TABLE songs ADD COLUMN archive_order INTEGER")
        self.db.commit()

    def rows(self, sql, args=()):
        # The connection is shared by HTTP and AutoPrep; never expose a
        # different thread's uncommitted writes through this read helper.
        with self.lock:
            return [dict(x) for x in self.db.execute(sql, args).fetchall()]

    def current(self):
        rows = self.rows(
            "SELECT * FROM songs WHERE status='playing' ORDER BY id LIMIT 1"
        )
        return rows[0] if rows else None

    def songs(self):
        rows = self.rows("""
            SELECT s.id,s.bvid,s.page,s.source_type,s.source_id,
                   s.title,s.uploader,s.cover,s.duration,
                   s.requested_at,s.status,s.media_file IS NOT NULL AS has_media,s.render_error,
                   s.prep_state,s.prep_error,s.prep_attempts,s.prep_retry_at,
                   s.skip_subtitles,s.prep_stage,s.prep_stage_at,s.priority_next,s.replay_of,
                   s.lyric_choice != '' AS lyric_selected,s.lyric_source,
                   s.lyric_timing_mode,s.lyric_offset_ms,
                   g.name AS nickname,g.id AS requester_id,s.lyrics != '' AS has_lyrics
            FROM songs s JOIN guests g ON g.id=s.requester
            WHERE s.status IN ('waiting','rendering','ready','playing')
            ORDER BY s.requested_at,s.id
        """)
        return [{**song, "lyric_query": title_key(song["title"])} for song in rows]

    def settings(self):
        values = {
            row["name"]: row["value"]
            for row in self.rows(
                "SELECT name,value FROM room_state "
                "WHERE name IN ('queue_policy','allow_unsubtitled','subtitle_style','video_quality','max_bandwidth_mbps','orders_paused','tv_html_overlay')"
            )
        }
        return {
            "queue_policy": values.get("queue_policy", "fair"),
            "allow_unsubtitled": values.get("allow_unsubtitled", "0") == "1",
            "subtitle_style": values.get("subtitle_style", "classic"),
            "video_quality": int(values.get("video_quality", "720")),
            "max_bandwidth_mbps": float(values.get("max_bandwidth_mbps", "3")),
            "orders_paused": values.get("orders_paused", "0") == "1",
            "tv_html_overlay": values.get("tv_html_overlay", "1") == "1",
        }

    def playback_mode(self):
        row = self.db.execute(
            "SELECT value FROM room_state WHERE name='playback_mode'"
        ).fetchone()
        return row[0] if row and row[0] in ("browser", "tv", "manual") else "tv"

    def save_settings(
        self,
        queue_policy: str | None,
        allow_unsubtitled: bool | None,
        subtitle_style: StylePreset | None = None,
        video_quality: Literal[720, 480] | None = None,
        max_bandwidth_mbps: float | None = None,
        orders_paused: bool | None = None,
        tv_html_overlay: bool | None = None,
    ):
        # Optional values are updates, not resets; legacy admin clients omit them.
        updates = {}
        if queue_policy is not None:
            updates["queue_policy"] = queue_policy
        if allow_unsubtitled is not None:
            updates["allow_unsubtitled"] = "1" if allow_unsubtitled else "0"
        for key, value in (
            ("subtitle_style", subtitle_style),
            ("video_quality", video_quality),
            ("max_bandwidth_mbps", max_bandwidth_mbps),
            ("orders_paused", None if orders_paused is None else int(orders_paused)),
            (
                "tv_html_overlay",
                None if tv_html_overlay is None else int(tv_html_overlay),
            ),
        ):
            if value is not None:
                updates[key] = str(value)
        with self.lock:
            try:
                self.db.executemany(
                    "INSERT INTO room_state(name,value) VALUES(?,?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    updates.items(),
                )
                self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise
            return self.settings()

    @staticmethod
    def playable(song, settings):
        return (
            song["status"] == "ready"
            and song["has_media"]
            and (
                settings["allow_unsubtitled"]
                or song.get("skip_subtitles", 0)
                or song["prep_state"] in ("done", "manual")
            )
        )

    def fair_queue(self, pool=None, settings=None, *, persist=False):
        """Order ready songs with at most two turns of accumulated catch-up.

        Preview is read-only; persist virtual credit only when claiming a song.
        Already completed performances remain a separate real-world statistic.
        """
        if pool is None:
            pool = self.songs()
        if settings is None:
            settings = self.settings()
        ready = [s for s in pool if self.playable(s, settings)]
        # The single admin-selected next song precedes ordinary fair/FCFS ordering.
        priority = [s for s in ready if s["priority_next"]]
        ready = [s for s in ready if not s["priority_next"]]
        if settings["queue_policy"] == "fcfs":
            return priority + ready  # songs() already orders by requested_at/id.
        users = {x["id"]: x for x in self.rows("SELECT * FROM guests")}
        # Simulate future turns so the displayed order reflects selection policy.
        tally = {k: u["plays"] + u["fair_offset"] for k, u in users.items()}
        # During playback, project the ordinary finish/skip turn before
        # predicting the next claim. Preview must never persist that turn.
        current = self.current() if not persist else None
        if current and not current["fair_exempt"]:
            tally[current["requester"]] += 1
        # Only people with a playable song can compete for a turn. Historical
        # inactivity must not accumulate unlimited "missing" rounds.
        # Admin-chosen priority songs are fictitious admin turns. They cannot
        # advance anybody's virtual count, including via floor normalization.
        active = {song["requester_id"] for song in ready}
        if active:
            floor = max(0, max(tally[user] for user in active) - 2)
            for user in active:
                if tally[user] < floor:
                    delta = floor - tally[user]
                    tally[user] = floor
                    if persist and not priority:
                        self.db.execute(
                            "UPDATE guests SET fair_offset=fair_offset+? WHERE id=?",
                            (delta, user),
                        )
        last = self.db.execute(
            "SELECT value FROM room_state WHERE name='last_requester'"
        ).fetchone()
        previous = last[0] if last else ""
        if current and not current["fair_exempt"]:
            previous = current["requester"]
        output = []
        while ready:
            head = {}
            for s in ready:
                head.setdefault(s["requester_id"], s)
            chosen = min(
                head.values(),
                key=lambda s: (
                    tally[s["requester_id"]],
                    s["requester_id"] == previous,
                    s["requested_at"],
                    s["id"],
                ),
            )
            output.append(chosen)
            tally[chosen["requester_id"]] += 1
            previous = chosen["requester_id"]
            ready.remove(chosen)
        return priority + output

    @staticmethod
    def downloadable(song):
        return (
            song["status"] in ("ready", "playing", "played", "skipped")
            and song["prep_state"] in ("done", "manual", "original")
            and bool(song.get("has_media") or song.get("media_file"))
        )

    def close_song_order(self):
        """Freeze actual turns, then the fair preview, then cancellations."""
        songs = self.rows(
            "SELECT id,status,played_at,requested_at FROM songs ORDER BY requested_at,id"
        )
        finished = sorted(
            (s for s in songs if s["status"] in ("played", "skipped")),
            key=lambda s: (s["played_at"] or 0, s["id"]),
        )
        active = [s for s in songs if s["status"] == "playing"]
        # Predict the full unplayed order, including tracks still preparing.
        # Pretend all pending tracks are playable, without persisting fair credit.
        unplayed_pool = [
            {**s, "status": "ready", "has_media": 1, "prep_state": "done"}
            for s in self.songs()
            if s["status"] in ("ready", "waiting", "rendering")
        ]
        predicted = self.fair_queue(unplayed_pool)
        cancelled = [s for s in songs if s["status"] == "cancelled"]
        ordered = [
            *(s["id"] for s in finished),
            *(s["id"] for s in active),
            *(s["id"] for s in predicted),
            *(s["id"] for s in cancelled),
        ]
        # Cover any legacy status with a stable position as well.
        included = set(ordered)
        ordered.extend(s["id"] for s in songs if s["id"] not in included)
        return ordered

    def claim_next(self):
        """Claim the first playable turn, leaving an existing player untouched.

        Caller holds the relevant lock: event-scoped for HTTP/TV routes,
        Store.lock when used directly.
        """
        if not self.current():
            try:
                queue = self.fair_queue(persist=True)
                if queue:
                    song = queue[0]
                    # Preserve an admin-pinned turn's fair-count exemption.
                    self.db.execute(
                        "UPDATE songs SET status='playing',priority_next=0,fair_exempt=? WHERE id=?",
                        (int(bool(song["priority_next"])), song["id"]),
                    )
                    self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise

    def finish_current(self, song_id: int, *, skipped: bool = False) -> int | None:
        """Settle a turn and return ready count, or None on a stale claim.

        Caller holds the relevant lock across settlement and response.
        Real performances and skipped turns have distinct counters; admin
        priority turns are exempt from both. HTTP errors belong to routes.
        """
        song = self.current()
        if not song or song["id"] != song_id:
            return None
        try:
            self.db.execute(
                "UPDATE songs SET status=?,played_at=? WHERE id=?",
                ("skipped" if skipped else "played", time_now(), song_id),
            )
            if not song["fair_exempt"]:
                if skipped:
                    # A skipped ordinary turn consumes only virtual song credit.
                    self.db.execute(
                        "UPDATE guests SET fair_offset=fair_offset+1 WHERE id=?",
                        (song["requester"],),
                    )
                else:
                    self.db.execute(
                        "UPDATE guests SET plays=plays+1,seconds=seconds+? WHERE id=?",
                        (song["duration"], song["requester"]),
                    )
                self.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('last_requester',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (song["requester"],),
                )
            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            raise
        return len(self.fair_queue())

    def state(self):
        with self.lock:
            pool = self.songs()
            for song in pool:
                song["downloadable"] = self.downloadable(song)
            settings = self.settings()
            return {
                "current": next((s for s in pool if s["status"] == "playing"), None),
                "queue": self.fair_queue(pool, settings),
                "waiting": [
                    {**s, "awaiting_subtitles": s["status"] == "ready"}
                    for s in pool
                    if s["status"] in ("waiting", "rendering")
                    or (s["status"] == "ready" and not self.playable(s, settings))
                ],
                "guests": self.rows(
                    "SELECT name,plays,seconds FROM guests ORDER BY name"
                ),
                "playback_mode": self.playback_mode(),
                "server_time": time_now(),
            }


async def bilibili_lookup(bvid: str, page: int) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 VC-KTV/0.1",
        "Referer": "https://www.bilibili.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            res = await client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=headers,
            )
            res.raise_for_status()
            envelope = res.json()
    except (httpx.HTTPError, ValueError) as e:
        bail(502, f"B 站视频信息暂时不可用：{type(e).__name__}")
    if envelope.get("code") != 0 or not envelope.get("data"):
        bail(404, "B 站未找到该视频，可能已删除或需要登录")
    d = envelope["data"]
    pages = d.get("pages") or [
        {"page": 1, "part": d.get("title", ""), "duration": d.get("duration", 0)}
    ]
    if not 1 <= page <= len(pages):
        bail(400, f"此视频只有 {len(pages)} P")
    p = pages[page - 1]
    duration = int(p.get("duration") or d.get("duration") or 0)
    if duration <= 0:
        bail(400, "视频时长无效")
    return {
        "bvid": d.get("bvid", bvid),
        "page": page,
        "title": d.get("title", "未命名视频")[:200],
        "part": p.get("part", ""),
        "pages": [
            {"page": q.get("page", i + 1), "part": q.get("part", "")}
            for i, q in enumerate(pages)
        ],
        "uploader": d.get("owner", {}).get("name", "未知 UP 主")[:100],
        "cover": re.sub(
            r"^http://([a-zA-Z0-9.-]+\.hdslb\.com)/",
            r"https://\1/",
            d.get("pic", ""),
        ),
        "duration": duration,
        "official_url": f"https://www.bilibili.com/video/{quote(bvid)}/?p={page}",
        "embed_url": f"https://player.bilibili.com/player.html?bvid={quote(bvid)}&page={page}&high_quality=1",
    }


async def vcpedia_find(query: str):
    if not query.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://vcpedia.cn/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query[:80],
                    "srlimit": 5,
                    "format": "json",
                },
                headers={"User-Agent": "VC-KTV/0.1 (song metadata lookup)"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            {
                "title": entry["title"],
                "url": "https://vcpedia.cn/" + quote(entry["title"].replace(" ", "_")),
            }
            for entry in data.get("query", {}).get("search", [])
        ]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def create_app(
    root: Path | None = None,
    password: str | None = None,
    secret: bytes | None = None,
    auto_prepare: bool | None = None,
    gpu_alignment: bool | None = None,
) -> FastAPI:
    root = Path(root or os.getenv("VC_KTV_DATA", "~/.local/share/vc-ktv")).expanduser()
    config = Path(os.getenv("VC_KTV_CONFIG", "~/.config/vc-ktv")).expanduser()
    if password is None:
        f = config / "admin-password"
        if not f.exists():
            raise RuntimeError(f"Missing {f}; run scripts/setup.sh first")
        password = f.read_text().strip()
    if secret is None:
        f = config / "token-secret"
        if not f.exists():
            raise RuntimeError(f"Missing {f}; run scripts/setup.sh first")
        secret = bytes.fromhex(f.read_text().strip())
    events = Events(root, Store)
    store = CurrentStore(events)
    media_pins = MediaPins(root)
    if auto_prepare is None:
        auto_prepare = os.getenv("VC_KTV_AUTOPREPARE", "1") != "0"
    if gpu_alignment is None:
        gpu_alignment = os.getenv("VC_KTV_GPU_ALIGNMENT", "1") != "0"
    worker = AutoPrep(
        events.active() if events.active_id else store,
        probe_media,
        gpu_alignment=gpu_alignment,
    )
    netease_login = NeteaseLogin(
        storage_path=root / ".private" / "netease-session.json"
    )
    worker.netease_cookies = netease_login.cookies
    running = False

    async def monitor_workers():
        # Restart missing workers without requiring a service restart or a
        # second scheduler. Failed SQLite recovery is retried by start().
        while True:
            await asyncio.sleep(10)
            with events.lock:
                if not running or not events.active_id or events.transitioning:
                    continue
                live = sum(thread.is_alive() for thread in worker.workers)
                if live >= worker.max_workers:
                    continue
                try:
                    if live:
                        worker.resume_after_failed_stop()
                    else:
                        worker.start()
                except Exception:
                    logging.getLogger(__name__).exception("AutoPrep recovery failed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal running
        if auto_prepare:
            from ktv_app.font_support import primary_font_warning

            warning = primary_font_warning("Noto Sans CJK SC")
            if warning:
                logging.getLogger(__name__).warning("KTV subtitle font: %s", warning)
        running = True
        if auto_prepare and events.active_id:
            worker.store = (
                events.active()
            )  # Also handle events created before lifespan.
            worker.start()
        monitor = asyncio.create_task(monitor_workers()) if auto_prepare else None
        try:
            yield
        finally:
            running = False
            if monitor:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            if auto_prepare and worker.worker:
                worker.stop()
            netease_login.close()

    app = FastAPI(title="VC KTV MVP", version="0.5.0", lifespan=lifespan)
    app.state.autoprep = worker
    app.state.media_pins = media_pins
    app.state.store = store
    app.state.events = events
    app.state.lookup = bilibili_lookup
    app.state.netease_lookup = lookup_netease_song
    app.state.netease_resolve = resolve_song_reference
    app.state.netease_search = search_netease_songs
    app.state.netease_login = netease_login
    app.state.lyrics_lookup = vcpedia_find
    app.state.candidate_search = search_lyric_candidates
    app.state.netease_lyrics = netease_song_lyrics
    lyric_search_times = {}
    failed_login = {}
    failed_invites = {}
    failed_users = {}
    trusted_proxy_ips = {
        str(ipaddress.ip_address(value.strip()))
        for value in os.getenv("VC_KTV_TRUSTED_PROXY_IPS", "").split(",")
        if value.strip()
    }

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if peer in trusted_proxy_ips:
            # The trusted proxy MUST overwrite X-Real-IP, not forward a user
            # supplied value. Never trust forwarding headers from other peers.
            forwarded = request.headers.get("x-real-ip", "")
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return peer

    def pinned_media_response(path: Path, **kwargs) -> PinnedFileResponse:
        # Caller holds store.lock while choosing the path and making this hard
        # link. FileResponse opens the file only after its route returns.
        pinned = media_pins.pin(path)
        return PinnedFileResponse(pinned, **kwargs)

    def check_room_capacity():
        count = store.db.execute(
            "SELECT COUNT(*) FROM songs WHERE status IN ('waiting','rendering','ready','playing')"
        ).fetchone()[0]
        if count >= MAX_ACTIVE_SONGS:
            bail(409, "本场待处理歌曲已满，请稍后再点歌")
        try:
            store.disk_budget.check()
        except DiskBudgetExceeded as error:
            bail(507, str(error))

    def check_new_order(guest_id: str):
        if store.settings()["orders_paused"]:
            bail(409, "本场已暂停点歌")
        check_room_capacity()
        count = store.db.execute(
            "SELECT COUNT(*) FROM songs WHERE requester=? "
            "AND status IN ('waiting','rendering','ready','playing')",
            (guest_id,),
        ).fetchone()[0]
        if count >= MAX_USER_SONGS:
            bail(409, "每人最多保留十首待唱歌曲")

    app.state.probe = probe_media
    render_slot = threading.BoundedSemaphore(1)
    rendering = threading.Event()
    manual_render_threads: dict[int, int] = {}
    manual_render_lock = threading.RLock()

    def stop_manual_renders() -> None:
        with manual_render_lock:
            for tid in list(manual_render_threads.values()):
                terminate_thread_children(tid)

    worker.cancel_external_tasks = stop_manual_renders

    def stop_song_processing(song_id: int) -> None:
        worker.cancel_song(song_id)
        with manual_render_lock:
            if tid := manual_render_threads.get(song_id):
                terminate_thread_children(tid)

    def sign(kind: str, value: str, ttl: int) -> str:
        expiry = str(time_now() + ttl)
        payload = f"{kind}.{value}.{expiry}"
        sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + sig

    def read_signed(token: str | None, kind: str) -> str | None:
        if not token:
            return None
        try:
            k, value, exp, mac = token.split(".")
            msg = f"{k}.{value}.{exp}"
            correct = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()
            if (
                k == kind
                and int(exp) >= time_now()
                and hmac.compare_digest(mac, correct)
            ):
                return value
        except (ValueError, TypeError):
            pass
        return None

    def require_guest(token):
        raw = read_signed(token, "guest")
        prefix = events.active_id + ":" if events.active_id else ""
        return raw[len(prefix) :] if raw and prefix and raw.startswith(prefix) else None

    def current_user(token):
        guest = require_guest(token)
        if not guest or not events.active_id:
            return None
        with store.lock:
            row = store.db.execute(
                "SELECT g.id,g.name FROM users u JOIN guests g ON g.id=u.id WHERE u.id=?",
                (guest,),
            ).fetchone()
        return dict(row) if row else None

    def require_user(token):
        user = current_user(token)
        if not user:
            bail(401, "请先加入本场 KTV 并登录")
        return user

    def require_entry(token):
        if not events.active_id or read_signed(token, "entry") != events.active_id:
            bail(401, "请先输入本场六位加入码")

    def user_cookie(response, user_id, event_id):
        ttl = 7 * 86400
        response.set_cookie(
            "vc_guest",
            sign("guest", f"{event_id}:{user_id}", ttl),
            max_age=ttl,
            httponly=True,
            samesite="strict",
            path="/",
        )

    def admin_principal(token):
        """Validate scope and current account revision on every request."""
        identity = read_signed(token, "admin")
        if identity == "yes":
            return {"role": "global"}
        if not identity or not events.active_id:
            return None
        parts = identity.split(":")
        if len(parts) != 3 or parts[0] != events.active_id:
            return None
        event_id, account_id, revision = parts
        with store.lock:
            row = store.db.execute(
                "SELECT id,username,revision FROM event_admins WHERE id=?",
                (account_id,),
            ).fetchone()
        if row is None or str(row["revision"]) != revision:
            return None
        return {
            "role": "event",
            "event_id": event_id,
            "id": account_id,
            "username": row["username"],
            "revision": row["revision"],
        }

    def require_admin(token):
        principal = admin_principal(token)
        if principal is None:
            bail(401, "需要管理员登录")
        return principal

    def require_global_admin(token):
        principal = require_admin(token)
        if principal["role"] != "global":
            bail(403, "仅全局管理员可操作")
        return principal

    def signed_media(song: dict) -> str:
        return f"/media/{song['id']}?token={quote(sign('media', f'{events.active_id}:{song["id"]}', 8 * 3600))}"

    def overlay_bundle(song: dict):
        """Find the latest completed source-matching bundle, never stale attempts."""
        if not song.get("source_file") or song.get("prep_state") != "done":
            return None
        root = store.media / f"{song['id']}-auto-alignment"
        for attempt in sorted(root.glob("attempt-*/attempt.json"), reverse=True):
            try:
                info = json.loads(attempt.read_text(encoding="utf-8"))
                if (
                    info.get("outcome") != "rendered"
                    or info.get("source_file") != song["source_file"]
                ):
                    continue
                for name in ("pipeline/subtitles", "provider_subtitles"):
                    folder = attempt.parent / name
                    if all(
                        (folder / f).is_file()
                        for f in ("timeline.json", "display_plan.json", "style.json")
                    ):
                        return folder
            except (OSError, ValueError, TypeError):
                continue
        return None

    def overlay_archive(bundle: Path) -> Path:
        return bundle.parent.parent if bundle.name == "subtitles" else bundle.parent

    def overlay_view(song: dict):
        if not store.settings()["tv_html_overlay"]:
            return None
        source = store.media / Path(song.get("source_file") or "").name
        bundle = overlay_bundle(song)
        if (
            not source.is_file()
            or not bundle
            or not float(song.get("duration") or 0) > 0
        ):
            return None
        # Never stream a high-bitrate source over the 4 Mbps proxy by accident.
        limit = store.settings()["max_bandwidth_mbps"]
        if source.stat().st_size * 8 > float(song["duration"] or 0) * limit * 800_000:
            cache = overlay_archive(bundle) / "tv-source.mp4"
            if not cache.is_file():
                return None
            try:
                metadata = json.loads(
                    (overlay_archive(bundle) / "attempt.json").read_text()
                )
                if float(metadata.get("max_bandwidth_mbps", 0)) > limit:
                    return None
            except (OSError, ValueError, TypeError):
                return None
        media_token = quote(sign("media", f"{events.active_id}:{song['id']}", 8 * 3600))
        tv_token = quote(sign("overlay", f"{events.active_id}:{song['id']}", 8 * 3600))
        return {
            "source_url": f"/media/{song['id']}/source?token={media_token}",
            "timeline_url": f"/api/tv/subtitles/{song['id']}?token={tv_token}",
        }

    def active_view():
        song = store.current()
        if not song:
            return None
        info = store.rows(
            """SELECT s.*, g.name AS nickname FROM songs s
                             JOIN guests g ON g.id=s.requester WHERE s.id=?""",
            (song["id"],),
        )[0]
        info["media_url"] = signed_media(info)
        info["html_overlay"] = overlay_view(info)
        return info

    def state_with_active():
        # The middleware pins the event; use only its Store mutex here so
        # an event-close request can set transitioning while readers drain.
        with events.active().lock:
            return {**store.state(), "active": active_view()}

    @app.middleware("http")
    async def origin_check(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                from urllib.parse import urlparse

                parsed = urlparse(origin)
                if parsed.netloc != request.headers.get(
                    "host"
                ) or parsed.scheme not in ("http", "https"):
                    return Response(
                        status_code=403, content="Cross-site request denied"
                    )
        path = request.url.path
        # Closing/creating the event are writers, not readers. Other API and
        # media routes cannot switch Store halfway through a response.
        lifecycle_writer = request.method == "POST" and path in {
            "/api/admin/events",
            "/api/admin/events/close",
        }
        if (
            not (path.startswith("/api/") or path.startswith("/media/"))
            or lifecycle_writer
        ):
            return await call_next(request)
        if not events.begin_request():
            return Response("场次正在切换，请重试", status_code=409)
        try:
            return await call_next(request)
        finally:
            events.end_request()

    @app.get("/api/event")
    def current_event():
        meta = events.metadata()
        return {"event": {"id": meta["id"], "name": meta["name"]} if meta else None}

    def archived_guest_event(token: str | None):
        identity = read_signed(token, "guest")
        if not identity or ":" not in identity:
            return None
        event_id, user_id = identity.split(":", 1)
        if event_id == events.active_id:
            return None
        with events.lock:
            event = next(
                (
                    e
                    for e in events.list()
                    if e["id"] == event_id and e["status"] == "closed"
                ),
                None,
            )
            if not event:
                return None
            try:
                path = events.database_path(event_id)
                with sqlite3.connect(
                    f"file:{quote(str(path), safe='/')}?mode=ro", uri=True
                ) as db:
                    row = db.execute(
                        "SELECT username FROM users WHERE id=?", (user_id,)
                    ).fetchone()
            except sqlite3.Error:
                return None
            return (
                {"id": event_id, "name": event["name"], "username": row[0]}
                if row
                else None
            )

    @app.get("/api/entry")
    def entry(
        vc_join: str | None = Cookie(default=None),
        vc_guest: str | None = Cookie(default=None),
    ):
        event = events.metadata()
        archive = archived_guest_event(vc_guest)
        if not event:
            return {
                "event": None,
                "joined": False,
                "authenticated": False,
                "username": None,
                "archived_event": archive,
            }
        user = current_user(vc_guest)
        joined = bool(user or read_signed(vc_join, "entry") == events.active_id)
        return {
            "event": {"id": event["id"], "name": event["name"]},
            "joined": joined,
            "authenticated": bool(user),
            "username": user["name"] if user else None,
            "archived_event": archive if not user else None,
        }

    @app.post("/api/join")
    def join_event(body: JoinEvent, request: Request, response: Response):
        info = events.metadata()
        if not info or info.get("legacy") or not info.get("code"):
            bail(409, "请等待管理员创建新 KTV 场次")
        ip = client_ip(request)
        recent = [t for t in failed_invites.get(ip, []) if t > time_now() - 300]
        if len(recent) >= 10:
            bail(429, "尝试过于频繁，请稍后重试")
        if (
            not body.code.isascii()
            or not body.code.isdecimal()
            or not hmac.compare_digest(body.code, info["code"])
        ):
            recent.append(time_now())
            failed_invites[ip] = recent
            bail(401, "加入码错误")
        failed_invites.pop(ip, None)
        event_id = info["id"]
        if events.active_id != event_id:
            bail(409, "场次已变化，请重新输入加入码")
        response.set_cookie(
            "vc_join",
            sign("entry", event_id, 3600),
            max_age=3600,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"ok": True, "event": {"id": info["id"], "name": info["name"]}}

    @app.post("/api/users/register")
    def register_user(
        body: RegisterUser,
        response: Response,
        vc_join: str | None = Cookie(default=None),
    ):
        require_entry(vc_join)
        name = body.username.strip()
        if not 1 <= len(name) <= 24:
            bail(400, "用户名长度须为 1–24 字")
        if body.password != body.confirm_password:
            bail(400, "两次密码输入不一致")
        try:
            encoded = password_hash(body.password)
        except ValueError as error:
            bail(400, str(error))
        user_id = secrets.token_hex(12)
        with store.lock:
            require_entry(vc_join)
            event_id = events.active_id
            try:
                # Registration itself earns nothing. A user gets bounded
                # catch-up only when they actually enter a playable queue.
                store.db.execute(
                    "INSERT INTO guests(id,name) VALUES (?,?)", (user_id, name)
                )
                store.db.execute(
                    "INSERT INTO users(id,username,password_hash) VALUES (?,?,?)",
                    (user_id, name, encoded),
                )
                store.db.commit()
            except sqlite3.IntegrityError:
                store.db.rollback()
                bail(409, "此用户名已被本场其他用户使用")
            except sqlite3.Error:
                store.db.rollback()
                raise
        user_cookie(response, user_id, event_id)
        return {"ok": True, "username": name}

    @app.post("/api/users/login")
    def login_user(
        body: UserLogin,
        request: Request,
        response: Response,
        vc_join: str | None = Cookie(default=None),
    ):
        require_entry(vc_join)
        ip = client_ip(request)
        key = (events.active_id, ip, body.username.strip())
        recent = [t for t in failed_users.get(key, []) if t > time_now() - 300]
        if len(recent) >= 10:
            bail(429, "尝试过于频繁，请稍后重试")
        with store.lock:
            require_entry(vc_join)
            event_id = events.active_id
            row = store.db.execute(
                "SELECT id,username,password_hash FROM users WHERE username=?",
                (body.username.strip(),),
            ).fetchone()
        if not row or not verify_password(body.password, row["password_hash"]):
            recent.append(time_now())
            failed_users[key] = recent
            bail(401, "用户名或密码错误")
        failed_users.pop(key, None)
        user_cookie(response, row["id"], event_id)
        return {"ok": True, "username": row["username"]}

    @app.post("/api/users/logout")
    def logout_user(response: Response):
        response.delete_cookie("vc_guest", path="/")
        response.delete_cookie("vc_join", path="/")
        return {"ok": True}

    @app.get("/api/admin/netease/status")
    def netease_session_status(
        response: Response, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        response.headers["Cache-Control"] = "no-store"
        return netease_login.status()

    @app.post("/api/admin/netease/qr")
    def netease_session_qr(
        response: Response, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        response.headers["Cache-Control"] = "no-store"
        try:
            return netease_login.start_qr()
        except (httpx.HTTPError, NeteaseLoginError, ValueError):
            bail(502, "网易云登录二维码暂不可用")

    @app.post("/api/admin/netease/check")
    def netease_session_check(
        response: Response, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        response.headers["Cache-Control"] = "no-store"
        try:
            return netease_login.poll()
        except NeteaseLoginError as exc:
            bail(409, str(exc))
        except httpx.HTTPStatusError as exc:
            bail(502, f"网易云扫码接口返回 HTTP {exc.response.status_code}，请稍后再试")
        except (httpx.HTTPError, ValueError):
            bail(502, "网易云扫码接口网络异常，请稍后重试")

    @app.post("/api/admin/netease/import")
    def netease_session_import(
        body: ImportNetEaseCookie,
        response: Response,
        vc_admin: str | None = Cookie(default=None),
    ):
        require_global_admin(vc_admin)
        response.headers["Cache-Control"] = "no-store"
        try:
            return netease_login.import_cookie(body.music_u)
        except NeteaseLoginError as exc:
            bail(409, str(exc))
        except (httpx.HTTPError, ValueError):
            bail(502, "网易云登录状态验证暂不可用，请稍后再试")

    @app.post("/api/admin/netease/disconnect")
    def netease_session_disconnect(
        response: Response, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        response.headers["Cache-Control"] = "no-store"
        return netease_login.disconnect()

    @app.get("/api/admin/settings")
    def get_playback_settings(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            return store.settings()

    @app.put("/api/admin/settings")
    def update_playback_settings(
        body: PlaybackSettings, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        with store.lock:
            return store.save_settings(
                body.queue_policy,
                body.allow_unsubtitled,
                body.subtitle_style,
                body.video_quality,
                body.max_bandwidth_mbps,
                body.orders_paused,
                body.tv_html_overlay,
            )

    @app.get("/api/admin/events")
    def list_events(vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        return {
            "active": events.active_id,
            "events": events.list()
            if principal["role"] == "global"
            else [events.metadata()],
            "role": principal["role"],
            "username": principal.get("username"),
        }

    def event_song_records(event_id: str, token: str | None):
        principal = require_admin(token)
        if principal["role"] == "event" and event_id != events.active_id:
            bail(403, "无权查看其他场次历史")
        with events.lock:
            path = events.database_path(event_id)
            with sqlite3.connect(
                f"file:{quote(str(path), safe='/')}?mode=ro", uri=True
            ) as db:
                db.row_factory = sqlite3.Row
                columns = {
                    column[1] for column in db.execute("PRAGMA table_info(songs)")
                }
                frozen = (
                    "archive_order" in columns
                    and db.execute(
                        "SELECT 1 FROM songs WHERE archive_order IS NOT NULL LIMIT 1"
                    ).fetchone()
                )
                ordering = (
                    "ORDER BY CASE WHEN s.archive_order IS NULL THEN 1 ELSE 0 END, "
                    "s.archive_order,s.requested_at,s.id"
                    if frozen
                    else """ORDER BY
                        CASE WHEN s.status IN ('played','skipped') THEN 0
                             WHEN s.status IN ('playing','closed_interrupted') THEN 1
                             WHEN s.status='cancelled' THEN 3
                             ELSE 2 END,
                        CASE WHEN s.status IN ('played','skipped') THEN s.played_at END,
                        s.requested_at,s.id"""
                )
                songs = [
                    dict(row)
                    for row in db.execute(
                        """SELECT s.id,s.title,g.name AS requester,
                                  s.status,s.source_type,s.source_id,s.bvid,
                                  s.page,s.requested_at,s.played_at,s.uploader
                           FROM songs s JOIN guests g ON g.id=s.requester
                           """
                        + ordering
                    )
                ]
            if event_id == events.active_id:
                rank = {
                    song_id: i
                    for i, song_id in enumerate(events.active().close_song_order())
                }
                songs.sort(key=lambda row: rank.get(row["id"], len(rank)))
            return songs

    @app.get("/api/admin/events/{event_id}/songs")
    def event_song_history(event_id: str, vc_admin: str | None = Cookie(default=None)):
        return {"songs": event_song_records(event_id, vc_admin)}

    @app.get("/api/admin/events/{event_id}/songs.csv")
    def export_event_songs(event_id: str, vc_admin: str | None = Cookie(default=None)):
        songs = event_song_records(event_id, vc_admin)
        output = io.StringIO()
        writer = csv.writer(output)
        fields = (
            "title",
            "requester",
            "status",
            "source_type",
            "source_id",
            "bvid",
            "page",
            "requested_at",
            "played_at",
        )
        writer.writerow(("order", *fields))
        for index, song in enumerate(songs, 1):
            values = [song[field] for field in fields]
            # Prevent spreadsheet formula execution in text and provider fields.
            for column in (0, 1, 4, 5):
                if values[column] and str(values[column])[0] in "=+-@\t\r":
                    values[column] = "'" + str(values[column])
            writer.writerow((index, *values))
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="vc-ktv-{event_id}-songs.csv"'
            },
        )

    @app.post("/api/admin/events/{event_id}/release-media")
    def release_event_media(event_id: str, vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        with events.lock:
            events.closed_folder(event_id)
        csv_data = export_event_songs(event_id, vc_admin).body.decode("utf-8")
        return events.release_media(event_id, csv_data)

    @app.delete("/api/admin/events/{event_id}")
    def delete_event_record(event_id: str, vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        return events.delete_closed(event_id)

    @app.post("/api/admin/events")
    def create_event(body: EventCreate, vc_admin: str | None = Cookie(default=None)):
        nonlocal worker
        require_global_admin(vc_admin)
        with events.lock:
            entry = events.create(body.name, body.code)
            worker = AutoPrep(events.active(), probe_media)
            worker.netease_cookies = netease_login.cookies
            worker.cancel_external_tasks = stop_manual_renders
            app.state.autoprep = worker
            if running and auto_prepare:
                # A new event has no unfinished preparation to replay.
                # A failing thread must not strand the published event.
                worker.resume_after_failed_stop()
                if not worker.workers:
                    # The room is already committed. Report degraded worker
                    # availability via /api/health instead of returning a 503
                    # that makes the operator think room creation failed.
                    logging.getLogger(__name__).error(
                        "Event created, but no AutoPrep worker could start"
                    )
        return entry

    @app.post("/api/admin/events/close")
    def close_event(vc_admin: str | None = Cookie(default=None)):
        nonlocal worker
        require_admin(vc_admin)
        with events.lock:
            if not events.active_id:
                bail(409, "没有活动场次")
            # Block new readers before waiting on a concurrent Store snapshot;
            # an event close must not leave new requests entering indefinitely.
            events.transitioning = True
            try:
                # Wait without holding Events.lock so new requests see 409,
                # then inspect playback only after existing readers drain.
                if not events.idle.wait_for(lambda: events.inflight == 0, timeout=10):
                    bail(409, "仍有请求正在处理，请稍后重试")
                target = events.active()
                # Stop claims first, cancel active subprocess trees, then join
                # workers. Never close a SQLite connection used by a live thread.
                worker.stop_signal.set()
                with worker.active_tasks_lock:
                    active_ids = list(worker.active_tasks)
                try:
                    for song_id in active_ids:
                        worker.cancel_song(song_id)
                    stop_manual_renders()
                    if not worker.stop() or rendering.is_set():
                        bail(409, "后台任务尚未退出，请稍后重试")
                    # TemporaryDirectory normally handles this; remove leftovers
                    # from interrupted work only after all workers have exited.
                    for temporary in target.root.glob("vc-ktv-[0-9]*-*"):
                        if temporary.is_dir() and not temporary.is_symlink():
                            shutil.rmtree(temporary, ignore_errors=True)
                    with target.lock:
                        before = target.rows(
                            "SELECT id,status,played_at,archive_order FROM songs"
                        )
                        order = target.close_song_order()
                        try:
                            target.db.executemany(
                                "UPDATE songs SET archive_order=? WHERE id=?",
                                [(i, sid) for i, sid in enumerate(order, 1)],
                            )
                            target.db.execute(
                                "UPDATE songs SET status='closed_interrupted' "
                                "WHERE status='playing'"
                            )
                            target.db.execute(
                                "UPDATE songs SET status='closed_unplayed' "
                                "WHERE status IN ('waiting','ready','rendering')"
                            )
                            target.db.commit()
                        except BaseException:
                            target.db.rollback()
                            raise
                    try:
                        return events.close()
                    except BaseException:
                        if events.active_id and events.current_store is target:
                            with target.lock:
                                target.db.executemany(
                                    "UPDATE songs SET status=?,played_at=?,archive_order=? WHERE id=?",
                                    [
                                        (
                                            row["status"],
                                            row["played_at"],
                                            row["archive_order"],
                                            row["id"],
                                        )
                                        for row in before
                                    ],
                                )
                                target.db.commit()
                        raise
                except BaseException:
                    if running and auto_prepare and events.active_id:
                        if any(thread.is_alive() for thread in worker.workers):
                            # Continue surviving threads and replenish exited ones.
                            worker.resume_after_failed_stop()
                        else:
                            # Python threads cannot be restarted. Replace the
                            # stopped worker with the same event and settings.
                            resumed = AutoPrep(
                                events.active(),
                                worker.probe,
                                download=worker.download,
                                enhance=worker.enhance,
                                max_workers=worker.max_workers,
                                gpu_alignment=worker.gpu_alignment,
                            )
                            # No preparation task is running: failed close
                            # only needs a new worker pool, not startup replay.
                            resumed.resume_after_failed_stop()
                            worker = resumed
                            worker.netease_cookies = netease_login.cookies
                            worker.cancel_external_tasks = stop_manual_renders
                            app.state.autoprep = resumed
                    raise
            finally:
                events.transitioning = False

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "version": "0.5.0",
            "auto_prepare": auto_prepare,
            "autoprep_workers": sum(thread.is_alive() for thread in worker.workers),
            "gpu_alignment": gpu_alignment,
            "autoprep_ready": (
                auto_prepare
                and (
                    not events.active_id
                    or any(thread.is_alive() for thread in worker.workers)
                )
            ),
        }

    @app.get("/api/state")
    def state(
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if not events.active_id:
            return {
                "current": None,
                "queue": [],
                "waiting": [],
                "guests": [],
                "server_time": time_now(),
            }
        user = current_user(vc_guest)
        if user is None and admin_principal(vc_admin) is None:
            require_user(vc_guest)
        guest = user["id"] if user else None
        snapshot = store.state()
        for song in [*snapshot["queue"], *snapshot["waiting"]]:
            song["mine"] = bool(guest and song["requester_id"] == guest)
        if snapshot["current"]:
            snapshot["current"]["mine"] = bool(
                guest and snapshot["current"]["requester_id"] == guest
            )
        return snapshot

    @app.get("/api/preview")
    async def preview(
        bvid: str, page: int = 1, vc_guest: str | None = Cookie(default=None)
    ):
        require_user(vc_guest)
        return await app.state.lookup(get_bvid(bvid), page)

    @app.get("/api/netease/search")
    async def netease_search(
        q: str,
        page: int = 0,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if current_user(vc_guest) is None and admin_principal(vc_admin) is None:
            require_user(vc_guest)
        if not q.strip() or page < 0:
            bail(400, "请输入歌名或歌手")
        try:
            results = (
                await app.state.netease_search(q)
                if page == 0
                else await app.state.netease_search(q, page=page)
            )
            return {"songs": results}
        except (httpx.HTTPError, ValueError, TypeError):
            bail(502, "网易云搜索暂不可用")

    @app.get("/api/netease/preview")
    async def netease_preview(
        ref: str,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        # Read-only previews are available to joined users and either admin role.
        # Account connection and credential endpoints will be global-admin-only.
        if current_user(vc_guest) is None and admin_principal(vc_admin) is None:
            require_user(vc_guest)
        try:
            song_id = await app.state.netease_resolve(ref)
        except ValueError as error:
            bail(400, str(error))
        except httpx.HTTPError:
            bail(502, "网易云分享链接暂时无法解析")
        try:
            return await app.state.netease_lookup(song_id)
        except LookupError:
            bail(404, "网易云未找到该单曲")
        except (httpx.HTTPError, ValueError, TypeError):
            bail(502, "网易云歌曲信息暂不可用")

    @app.get("/api/lyrics/search")
    async def lyrics_search(query: str, vc_guest: str | None = Cookie(default=None)):
        require_user(vc_guest)
        return {"candidates": await app.state.lyrics_lookup(query)}

    @app.get("/api/admin/lyrics/vcpedia")
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

    @app.post("/api/songs")
    async def request_song(
        body: RequestSong,
        response: Response,
        vc_guest: str | None = Cookie(default=None),
    ):
        user = require_user(vc_guest)
        guest = user["id"]
        event_id = events.active_id
        with store.lock:
            check_new_order(guest)
        info = await app.state.lookup(get_bvid(body.bvid), body.page)
        with store.lock:
            if events.active_id != event_id:
                bail(409, "场次已变化，请在新场次重新点歌")
            # Recheck under the same lock as insertion: parallel requests
            # cannot overfill the room or bypass a newly enabled pause.
            check_new_order(guest)
            try:
                cur = store.db.execute(
                    """
                    INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        info["bvid"],
                        info["page"],
                        info["title"],
                        info["uploader"],
                        info["cover"],
                        info["duration"],
                        guest,
                        time_now(),
                    ),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {
            "id": cur.lastrowid,
            "status": "waiting",
            "title": info["title"],
            "lyric_query": title_key(info["title"]),
        }

    @app.post("/api/netease/songs")
    async def request_netease_song(
        body: RequestNetEaseSong,
        vc_guest: str | None = Cookie(default=None),
    ):
        user = require_user(vc_guest)
        try:
            song_id = await app.state.netease_resolve(body.ref)
        except ValueError as error:
            bail(400, str(error))
        except httpx.HTTPError:
            bail(502, "网易云分享链接暂时无法解析")
        event_id = events.active_id
        with store.lock:
            check_new_order(user["id"])
        try:
            info = await app.state.netease_lookup(song_id)
        except LookupError:
            bail(404, "网易云未找到该单曲")
        except (httpx.HTTPError, ValueError, TypeError):
            bail(502, "网易云歌曲信息暂不可用")
        with store.lock:
            if events.active_id != event_id:
                bail(409, "场次已变化，请重新点歌")
            check_new_order(user["id"])
            try:
                cur = store.db.execute(
                    """INSERT INTO songs(
                         bvid,page,title,uploader,cover,duration,requester,requested_at,
                         source_type,source_id
                       ) VALUES('',1,?,?,?,?,?,?,'netease',?)""",
                    (
                        info["title"],
                        info["uploader"],
                        info["cover"],
                        info["duration"],
                        user["id"],
                        time_now(),
                        str(song_id),
                    ),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {
            "id": cur.lastrowid,
            "status": "waiting",
            "title": info["title"],
            "lyric_query": title_key(info["title"]),
            "source": "netease",
        }

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

    @app.post("/api/songs/{song_id}/lyrics/skip")
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

    @app.get("/api/songs/{song_id}/lyrics/candidates")
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

    @app.post("/api/songs/{song_id}/lyrics/select")
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

    @app.post("/api/songs/{song_id}/lyrics/upload")
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

    @app.get("/api/admin/accounts")
    def list_event_admins(vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        with store.lock:
            return {
                "accounts": store.rows(
                    "SELECT id,username FROM event_admins ORDER BY username COLLATE NOCASE"
                )
            }

    @app.post("/api/admin/accounts", status_code=201)
    def create_event_admin(
        body: EventAdminCredentials, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        username = body.username.strip()
        if not username:
            bail(422, "管理员用户名不能为空")
        hashed = password_hash(body.password)
        with store.lock:
            account_id = secrets.token_hex(16)
            try:
                store.db.execute(
                    "INSERT INTO event_admins(id,username,password_hash) VALUES(?,?,?)",
                    (account_id, username, hashed),
                )
                store.db.commit()
            except sqlite3.IntegrityError:
                store.db.rollback()
                bail(409, "本场管理员用户名已存在")
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"id": account_id, "username": username}

    @app.put("/api/admin/accounts/{account_id}/password")
    def reset_event_admin_password(
        account_id: str,
        body: EventAdminPassword,
        vc_admin: str | None = Cookie(default=None),
    ):
        require_global_admin(vc_admin)
        hashed = password_hash(body.password)
        with store.lock:
            try:
                updated = store.db.execute(
                    "UPDATE event_admins SET password_hash=?,revision=revision+1 WHERE id=?",
                    (hashed, account_id),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        if not updated.rowcount:
            bail(404, "管理员账号不存在")
        return {"ok": True}

    @app.delete("/api/admin/accounts/{account_id}")
    def revoke_event_admin(
        account_id: str, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        with store.lock:
            try:
                deleted = store.db.execute(
                    "DELETE FROM event_admins WHERE id=?", (account_id,)
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        if not deleted.rowcount:
            bail(404, "管理员账号不存在")
        return {"ok": True}

    @app.post("/api/admin/login")
    def login(body: Login, request: Request, response: Response):
        ip = client_ip(request)
        key = (ip, body.username.strip() if body.username else "")
        recent = [t for t in failed_login.get(key, []) if t > time_now() - 300]
        if len(recent) >= 10:
            bail(429, "登录尝试过于频繁，请稍后重试")
        identity = None
        if body.username is None or not body.username.strip():
            if hmac.compare_digest(body.password, password):
                identity = "yes"
        elif events.active_id:
            with store.lock:
                account = store.db.execute(
                    "SELECT id,password_hash,revision FROM event_admins WHERE username=?",
                    (body.username.strip(),),
                ).fetchone()
                if (
                    account
                    and len(body.password) <= 72
                    and verify_password(body.password, account["password_hash"])
                ):
                    identity = (
                        f"{events.active_id}:{account['id']}:{account['revision']}"
                    )
        if identity is None:
            recent.append(time_now())
            failed_login[key] = recent
            bail(401, "用户名或密码错误")
        failed_login.pop(key, None)
        response.set_cookie(
            "vc_admin",
            sign("admin", identity, 7 * 86400),
            max_age=7 * 86400,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"ok": True}

    @app.post("/api/admin/logout")
    def logout(response: Response):
        response.delete_cookie("vc_admin", path="/")
        return {"ok": True}

    @app.get("/api/admin/state")
    def admin_state(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        if not events.active_id:
            return {
                "current": None,
                "queue": [],
                "waiting": [],
                "guests": [],
                "active": None,
            }
        return state_with_active()

    @app.post("/api/admin/songs/{song_id}/subtitles/retry")
    def retry_subtitles(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT status,prep_state,lyric_choice,source_file FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["status"] != "ready" or row["prep_state"] not in (
                "original",
                "done",
            ):
                bail(409, "仅可重制尚未播放且字幕已完成或失败的歌曲")
            if not row["lyric_choice"] or not row["source_file"]:
                bail(409, "歌曲缺少已选歌词或原始视频")
            if not (store.media / Path(row["source_file"]).name).is_file():
                bail(409, "原视频已丢失，无法仅重试字幕")
            try:
                store.db.execute(
                    "UPDATE songs SET prep_state='downloaded',prep_error='',"
                    "skip_subtitles=0,prep_stage='queued',prep_stage_at=? WHERE id=?",
                    (time_now(), song_id),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "status": "downloaded"}

    @app.post("/api/admin/songs/{song_id}/download/retry")
    def retry_media_download(song_id: int, vc_admin: str | None = Cookie(default=None)):
        """Requeue one failed download without requiring the guest to re-order."""
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT status,prep_state FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if row is None:
                bail(404, "歌曲不存在")
            if row["status"] != "waiting" or row["prep_state"] != "unavailable":
                bail(409, "只有准备失败的待处理歌曲可以重试下载")
            try:
                store.db.execute(
                    "UPDATE songs SET prep_state='pending',prep_attempts=0,"
                    "prep_retry_at=0,prep_error='',prep_stage='download' WHERE id=?",
                    (song_id,),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id}

    @app.post("/api/admin/songs/{song_id}/subtitles/skip")
    def skip_subtitles(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT status,prep_state,source_file,skip_subtitles "
                "FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if (
                row["skip_subtitles"]
                or row["status"] != "ready"
                or row["prep_state"]
                not in (
                    "downloaded",
                    "original",
                    "aligning",
                )
            ):
                bail(409, "仅可对已下载且尚未播放的歌曲跳过字幕")
            source = store.media / Path(row["source_file"] or "").name
            if not source.is_file():
                bail(409, "原视频尚未就绪，无法进入队列")
            try:
                store.db.execute(
                    "UPDATE songs SET media_file=source_file,prep_state='original',"
                    "skip_subtitles=1,prep_stage='skipped',prep_stage_at=?,"
                    "prep_error='' WHERE id=?",
                    (time_now(), song_id),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "status": "ready", "source": "original"}

    @app.post("/api/admin/songs/{song_id}/media")
    async def upload(
        song_id: int,
        file: Annotated[UploadFile, File()],
        vc_admin: str | None = Cookie(default=None),
    ):
        require_admin(vc_admin)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in MEDIA_MIMES:
            bail(400, "仅支持 MP4、M4V、WebM 文件")
        with store.lock:
            row = store.db.execute(
                "SELECT status,media_file,source_file FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["status"] not in ("waiting", "ready"):
                bail(409, "正在播放或已结束，不能覆盖视频")
        target = store.media / f"{song_id}-{secrets.token_hex(8)}{suffix}"
        size = 0
        try:
            with target.open("wb") as out:
                while block := await file.read(1024 * 1024):
                    size += len(block)
                    out.write(block)
                    # Include this file and all existing room artifacts in
                    # the aggregate 200 GiB budget, not just this upload.
                    if size % (16 * 1024 * 1024) < len(block):
                        try:
                            store.disk_budget.check()
                        except DiskBudgetExceeded as error:
                            bail(507, str(error))
            try:
                store.disk_budget.check()
            except DiskBudgetExceeded as error:
                bail(507, str(error))
            if not size:
                bail(400, "视频为空")
            try:
                actual_duration = await run_in_threadpool(app.state.probe, target)
            except ValueError as error:
                bail(400, str(error))
            with store.lock:
                row = store.db.execute(
                    "SELECT status,media_file,source_file FROM songs WHERE id=?",
                    (song_id,),
                ).fetchone()
                if row["status"] not in ("waiting", "ready"):
                    bail(409, "歌曲状态已变化")
                try:
                    store.db.execute(
                        "UPDATE songs SET status='ready',media_file=?,source_file=?,duration=?,render_error='',prep_state='manual',prep_error='' WHERE id=?",
                        (target.name, target.name, round(actual_duration), song_id),
                    )
                    store.db.commit()
                except BaseException:
                    # The outer handler must see the last committed media
                    # pointer before deciding whether to remove this upload.
                    store.db.rollback()
                    raise
            for old in set(filter(None, (row["media_file"], row["source_file"]))):
                if old != target.name:
                    try:
                        (store.media / old).unlink(missing_ok=True)
                    except OSError as error:
                        logging.getLogger(__name__).warning(
                            "Song #%s obsolete upload cleanup failed: %s",
                            song_id,
                            error,
                        )
        except BaseException:
            with store.lock:
                if not store.db.execute(
                    "SELECT 1 FROM songs WHERE id=? AND media_file=?",
                    (song_id, target.name),
                ).fetchone():
                    target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        return {"id": song_id, "status": "ready", "bytes": size}

    @app.post("/api/admin/songs/{song_id}/lyrics")
    def set_lyrics(
        song_id: int, body: Lyrics, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        with store.lock:
            if not store.db.execute(
                "SELECT 1 FROM songs WHERE id=?", (song_id,)
            ).fetchone():
                bail(404, "歌曲不存在")
            if (
                store.db.execute(
                    "SELECT status FROM songs WHERE id=?", (song_id,)
                ).fetchone()[0]
                == "rendering"
            ):
                bail(409, "字幕制作中暂不能修改歌词")
            try:
                store.db.execute(
                    "UPDATE songs SET lyrics=? WHERE id=?", (body.text, song_id)
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True}

    @app.get("/api/admin/songs/{song_id}/lyrics")
    def get_lyrics(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT lyrics FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            return {"text": row[0]}

    def render_worker(
        target_store: Store,
        song_id: int,
        source: Path,
        lyrics: str,
        duration: float,
        lead: float,
    ):
        destination = target_store.media / f"{song_id}-ktv-{secrets.token_hex(8)}.mp4"
        with manual_render_lock:
            manual_render_threads[song_id] = threading.get_native_id()
        try:
            with target_store.lock:
                current = target_store.db.execute(
                    "SELECT status FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if not current or current["status"] != "rendering":
                    return
            render_video(source, lyrics, destination, duration, lead)
            with target_store.lock:
                row = target_store.db.execute(
                    "SELECT media_file,status FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if row and row["status"] == "rendering":
                    try:
                        target_store.db.execute(
                            "UPDATE songs SET status='ready',media_file=?,render_error='' WHERE id=?",
                            (destination.name, song_id),
                        )
                        target_store.db.commit()
                    except BaseException:
                        # The outer handler deletes destination. Roll back the
                        # media pointer before it records the render failure.
                        target_store.db.rollback()
                        raise
                    # Publishing is complete; deleting the old video is only
                    # best effort and must never invalidate the new DB path.
                    if row["media_file"] and row["media_file"] != source.name:
                        try:
                            (target_store.media / row["media_file"]).unlink(
                                missing_ok=True
                            )
                        except OSError as error:
                            logging.getLogger(__name__).warning(
                                "Song #%s obsolete manual render cleanup failed: %s",
                                song_id,
                                error,
                            )
                else:
                    destination.unlink(missing_ok=True)
        except Exception as error:
            destination.unlink(missing_ok=True)
            with target_store.lock:
                for attempt in range(2):
                    try:
                        target_store.db.execute(
                            "UPDATE songs SET status='ready',render_error=? WHERE id=? AND status='rendering'",
                            (f"制作失败：{type(error).__name__}", song_id),
                        )
                        target_store.db.commit()
                        break
                    except sqlite3.Error:
                        target_store.db.rollback()
                        logging.getLogger(__name__).exception(
                            "Manual render failure writeback failed for song #%s (attempt %s)",
                            song_id,
                            attempt + 1,
                        )
        finally:
            with manual_render_lock:
                manual_render_threads.pop(song_id, None)
            rendering.clear()
            render_slot.release()

    @app.post("/api/admin/songs/{song_id}/alignment/import")
    async def import_textgrid(
        song_id: int,
        file: Annotated[UploadFile, File()],
        offset: float = 0,
        vc_admin: str | None = Cookie(default=None),
    ):
        require_admin(vc_admin)
        if not (file.filename or "").lower().endswith(".textgrid"):
            bail(400, "请上传 TIFA TextGrid 文件")
        content = await file.read()
        await file.close()
        with store.lock:
            row = store.db.execute(
                "SELECT status,lyrics,duration,source_file FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["status"] not in ("ready", "waiting") or not row["source_file"]:
                bail(409, "请先上传原始视频，且不能在播放或制作过程中替换时间轴")
            original = row["lyrics"]
            duration = row["duration"]
        try:
            with tempfile.TemporaryDirectory(prefix="vc-ktv-grid-") as temp:
                path = Path(temp) / "alignment.TextGrid"
                path.write_bytes(content)
                proposed, report = convert_textgrid(path, original, duration, offset)
                write_ass(proposed, Path(temp) / "validated.ass", duration, 1.8)
        except (ValueError, UnicodeError, OSError, AssertionError) as error:
            bail(400, f"TextGrid 无法安全导入：{error}")
        # Keep raw alignment and original text for reproducibility.
        archive = store.media / f"{song_id}-alignment-{secrets.token_hex(5)}"
        archive.mkdir(mode=0o700)
        try:
            (archive / "alignment.TextGrid").write_bytes(content)
            (archive / "lyrics.before.txt").write_text(original, encoding="utf-8")
            (archive / "lyrics.enhanced.lrc").write_text(proposed, encoding="utf-8")
            with store.lock:
                latest = store.db.execute(
                    "SELECT status,lyrics FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if (
                    not latest
                    or latest["status"] not in ("ready", "waiting")
                    or latest["lyrics"] != original
                ):
                    bail(409, "歌词或歌曲状态发生变化，请重新导入")
                try:
                    store.db.execute(
                        "UPDATE songs SET lyrics=? WHERE id=?", (proposed, song_id)
                    )
                    store.db.commit()
                except sqlite3.Error:
                    store.db.rollback()
                    raise
        except BaseException:
            import shutil

            shutil.rmtree(archive)
            raise
        return {"id": song_id, "report": report, "lyrics": proposed}

    @app.post("/api/admin/songs/{song_id}/render")
    def render_song(
        song_id: int, body: RenderRequest, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        if not render_slot.acquire(blocking=False):
            bail(409, "已有歌曲正在制作，请稍后再试")
        try:
            with store.lock:
                row = store.db.execute(
                    "SELECT * FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if not row:
                    bail(404, "歌曲不存在")
                if row["status"] != "ready" or not row["source_file"]:
                    bail(409, "请先上传可用视频")
                source = store.media / row["source_file"]
                if not source.is_file():
                    bail(404, "原视频不存在，请重新上传")
                try:
                    with tempfile.TemporaryDirectory(prefix="vc-ktv-validate-") as tmp:
                        write_ass(
                            row["lyrics"],
                            Path(tmp) / "lyrics.ass",
                            row["duration"],
                            body.lead,
                        )
                except ValueError as error:
                    bail(400, str(error))
                try:
                    store.db.execute(
                        "UPDATE songs SET status='rendering',render_error='' WHERE id=?",
                        (song_id,),
                    )
                    store.db.commit()
                except BaseException:
                    store.db.rollback()
                    raise
                rendering.set()
                try:
                    render_thread = threading.Thread(
                        target=render_worker,
                        args=(
                            events.active(),
                            song_id,
                            source,
                            row["lyrics"],
                            row["duration"],
                            body.lead,
                        ),
                        daemon=True,
                    )
                    render_thread.start()
                except BaseException:
                    # No worker was launched to clear rendering or release the
                    # slot. Restore the state before returning an error.
                    rendering.clear()
                    store.db.execute(
                        "UPDATE songs SET status='ready' WHERE id=? AND status='rendering'",
                        (song_id,),
                    )
                    store.db.commit()
                    raise
                return {"id": song_id, "status": "rendering", "lead": body.lead}
        except BaseException:
            render_slot.release()
            raise

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

    @app.put("/api/admin/playback-mode")
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

    @app.get("/api/admin/played")
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

    @app.post("/api/admin/songs/{song_id}/pin-next")
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
                or not row["media_file"]
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
            media = store.media / Path(row["media_file"]).name
            if not media.is_file():
                bail(404, "成片已丢失")
            # Admin replay still works during pause, but cannot exceed the
            # same global unfinished-song capacity.
            check_room_capacity()
            source = store.media / Path(row["source_file"] or "").name
            if not source.is_file():
                source = media
            # Unique hard links isolate later per-song edits without copying MP4 bytes.
            links = []
            try:
                if guest_id is None:
                    store.db.execute(
                        "UPDATE songs SET priority_next=0 WHERE priority_next=1"
                    )
                cur = store.db.execute(
                    """INSERT INTO songs(
                        bvid,page,title,uploader,cover,duration,requester,requested_at,
                        status,lyrics,lyric_choice,lyric_source,prep_state,
                        skip_subtitles,priority_next,replay_of,source_type,source_id
                    ) VALUES(?,?,?,?,?,?,?,?,'ready',?,?,?,?,?,?,?,?,?)""",
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
                    ),
                )
                new_id = cur.lastrowid
                for label, old in (("video", media), ("source", source)):
                    if label == "source" and old == media:
                        continue
                    new_path = store.media / (
                        f"{new_id}-replay-{secrets.token_hex(5)}-{label}{old.suffix}"
                    )
                    os.link(old, new_path)
                    links.append(new_path)
                new_media = links[0].name
                new_source = links[-1].name if source != media else new_media
                store.db.execute(
                    "UPDATE songs SET media_file=?,source_file=?,lyric_timing_mode=?,"
                    "provider_lrc=?,source_lrc_raw=?,lyric_offset_ms=? WHERE id=?",
                    (
                        new_media,
                        new_source,
                        row["lyric_timing_mode"],
                        row["provider_lrc"],
                        row["source_lrc_raw"],
                        row["lyric_offset_ms"],
                        new_id,
                    ),
                )
                store.db.commit()
            except BaseException:
                store.db.rollback()
                for link in links:
                    link.unlink(missing_ok=True)
                raise
        return {"id": new_id, "status": "ready", "priority_next": guest_id is None}

    @app.post("/api/songs/{song_id}/replay")
    def replay_own_turn(song_id: int, vc_guest: str | None = Cookie(default=None)):
        user = require_user(vc_guest)
        return schedule_replay(song_id, guest_id=user["id"])

    @app.post("/api/admin/songs/{song_id}/replay")
    def replay_song(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return schedule_replay(song_id)

    @app.post("/api/admin/songs/{song_id}/replay-next")
    def replay_current(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "电视模式请使用主屏遥控器")
            return schedule_replay(song_id, current_only=True)

    @app.post("/api/admin/next")
    def next_song(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "当前由电视主屏播放")
            return next_impl()

    @app.post("/api/admin/finish")
    def finish_song(body: Finish, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "当前由电视主屏播放")
            return finish_impl(body)

    def tv_record(name: str) -> dict | None:
        row = store.db.execute(
            "SELECT value FROM room_state WHERE name=?", (name,)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def tv_save(name: str, value: dict | None):
        if value is None:
            store.db.execute("DELETE FROM room_state WHERE name=?", (name,))
        else:
            store.db.execute(
                "INSERT INTO room_state(name,value) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, json.dumps(value)),
            )

    def tv_remote_record() -> dict:
        return tv_record("tv_remote") or {
            "seq": 0,
            "ack": 0,
            "command": None,
            "issued": 0,
            "seen": 0,
            "report": {},
        }

    def tv_owner_valid(record: dict) -> bool:
        if record["role"] == "global":
            return True
        row = store.db.execute(
            "SELECT revision FROM event_admins WHERE id=?", (record["owner_id"],)
        ).fetchone()
        return bool(row and row["revision"] == record["owner_revision"])

    def tv_can_manage(principal: dict, record: dict | None) -> bool:
        return (
            record is None or principal["role"] == "global" or record["role"] == "event"
        )

    def require_tv(token: str):
        # A TV URL only contains a one-time pairing ticket. Control credentials
        # are created when the page connects, and are never in shareable links.
        identity = read_signed(token, "tv-control")
        if not identity or not events.active_id:
            bail(401, "主屏权限已失效，请管理员重新连接")
        event_id, separator, nonce = identity.partition(":")
        if not separator or event_id != events.active_id:
            bail(401, "主屏权限已失效，请管理员重新连接")
        with store.lock:
            active = tv_record("tv_primary")
            if not active:
                bail(401, "主屏已被撤回，请管理员重新连接")
            if not hmac.compare_digest(
                active["nonce_hash"], hashlib.sha256(nonce.encode()).hexdigest()
            ) or not tv_owner_valid(active):
                bail(401, "此页面不再是主屏，可能已由另一设备接管")

    @app.get("/api/admin/tv")
    def tv_management(vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        if not events.active_id:
            return {"active": False, "owner_role": None, "can_manage": False}
        with store.lock:
            active = tv_record("tv_primary")
            return {
                "active": bool(active),
                "owner_role": active["role"] if active else None,
                "can_manage": tv_can_manage(principal, active),
            }

    @app.get("/api/admin/tv/remote")
    def tv_remote_state(vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        with store.lock:
            primary = tv_record("tv_primary")
            remote = tv_remote_record()
            return {
                "online": bool(primary and remote["seen"] > time_now() - 15),
                "can_manage": tv_can_manage(principal, primary),
                "report": remote["report"],
                "pending": bool(
                    remote["seq"] > remote["ack"] and remote["issued"] > time_now() - 20
                ),
            }

    @app.post("/api/admin/tv/remote")
    def tv_remote_send(
        body: TVRemoteCommand, vc_admin: str | None = Cookie(default=None)
    ):
        principal = require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            primary = tv_record("tv_primary")
            if not primary or not tv_owner_valid(primary):
                bail(409, "主屏未连接")
            if not tv_can_manage(principal, primary):
                bail(403, "无权遥控全局管理员的主屏")
            remote = tv_remote_record()
            if remote["seen"] < time_now() - 15:
                bail(409, "主屏离线")
            if remote["seq"] > remote["ack"] and remote["issued"] > time_now() - 20:
                bail(409, "上一条遥控命令尚未完成")
            song_actions = ("pause", "resume", "restart", "skip", "replay_next")
            if body.action in song_actions:
                current = store.current()
                if not current or body.song_id != current["id"]:
                    bail(409, "歌曲已切换，请刷新")
            else:
                limit = {"volume": (0, 100), "offset": (-600, 600), "opacity": (0, 100)}
                if body.action == "smooth":
                    if not isinstance(body.value, bool):
                        bail(400, "无效的稳播设置")
                elif (
                    not isinstance(body.value, (int, float))
                    or isinstance(body.value, bool)
                    or not limit[body.action][0] <= body.value <= limit[body.action][1]
                ):
                    bail(400, "遥控参数超出范围")
            seq = remote["seq"] + 1
            remote.update(
                seq=seq,
                issued=time_now(),
                command={
                    "seq": seq,
                    "action": body.action,
                    "song_id": body.song_id,
                    "value": body.value,
                },
            )
            tv_save("tv_remote", remote)
            store.db.commit()
            return {"seq": seq}

    @app.post("/api/tv/report")
    def tv_report(body: TVRemoteReport, token: str):
        require_tv(token)
        with store.lock:
            remote = tv_remote_record()
            if remote["ack"] <= body.ack_seq <= remote["seq"]:
                remote["ack"] = body.ack_seq
            remote["seen"] = time_now()
            remote["report"] = body.model_dump()
            tv_save("tv_remote", remote)
            store.db.commit()
            return {"ok": True}

    @app.get("/api/admin/tv-link")
    def tv_link(
        replace: bool = False,
        vc_admin: str | None = Cookie(default=None),
    ):
        principal = require_admin(vc_admin)
        events.active()
        with store.lock:
            active = tv_record("tv_primary")
            pending = tv_record("tv_pending")
            if not tv_can_manage(principal, active):
                bail(403, "本场管理员不能撤回全局管理员的主屏")
            if (
                pending
                and pending["expires"] > time_now()
                and not tv_can_manage(principal, pending)
            ):
                bail(403, "本场管理员不能覆盖全局管理员的配对链接")
            if active and not replace:
                bail(409, "已有主屏，请使用更换主屏")
            # Reusing a pending link must not silently invalidate earlier copies.
            if (
                pending
                and pending["expires"] > time_now()
                and pending["role"] == principal["role"]
                and pending["owner_id"] == principal.get("id", "")
                and pending["owner_revision"] == principal.get("revision", 0)
                and pending["replace"] == replace
                and pending.get("pair_token")
            ):
                return {"url": "/tv.html?pair=" + quote(pending["pair_token"])}
            nonce = secrets.token_urlsafe(24)
            pair = {
                "nonce_hash": hashlib.sha256(nonce.encode()).hexdigest(),
                "role": principal["role"],
                "owner_id": principal.get("id", ""),
                "owner_revision": principal.get("revision", 0),
                "replace": replace,
                "expires": time_now() + 600,
                "pair_token": sign("tv-pair", f"{events.active_id}:{nonce}", 600),
            }
            tv_save("tv_pending", pair)
            store.db.commit()
            return {"url": "/tv.html?pair=" + quote(pair["pair_token"])}

    @app.post("/api/tv/activate")
    def tv_activate(body: TVPair):
        identity = read_signed(body.pair, "tv-pair")
        if not identity or not events.active_id:
            bail(401, "主屏配对链接已过期，请管理员重新生成")
        event_id, sep, nonce = identity.partition(":")
        if not sep or event_id != events.active_id:
            bail(401, "主屏配对链接已过期，请管理员重新生成")
        with store.lock:
            pending = tv_record("tv_pending")
            active = tv_record("tv_primary")
            if (
                not pending
                or pending["expires"] < time_now()
                or not hmac.compare_digest(
                    pending["nonce_hash"], hashlib.sha256(nonce.encode()).hexdigest()
                )
                or not tv_owner_valid(pending)
            ):
                bail(409, "主屏配对链接已使用或失效，请管理员重新生成")
            if active and (
                not pending["replace"]
                or (active["role"] == "global" and pending["role"] != "global")
            ):
                bail(409, "主屏已被其他管理员占用，请重新连接")
            control_nonce = secrets.token_urlsafe(32)
            tv_save(
                "tv_primary",
                {
                    "nonce_hash": hashlib.sha256(control_nonce.encode()).hexdigest(),
                    "role": pending["role"],
                    "owner_id": pending["owner_id"],
                    "owner_revision": pending["owner_revision"],
                },
            )
            tv_save("tv_pending", None)
            tv_save("tv_remote", None)
            store.db.commit()
            return {
                "token": sign(
                    "tv-control", f"{events.active_id}:{control_nonce}", 8 * 3600
                )
            }

    @app.post("/api/admin/tv/revoke")
    def tv_revoke(vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        with store.lock:
            active = tv_record("tv_primary")
            pending = tv_record("tv_pending")
            if not tv_can_manage(principal, active):
                bail(403, "本场管理员不能撤回全局管理员的主屏")
            if not active and not tv_can_manage(principal, pending):
                bail(403, "本场管理员不能撤回全局管理员的配对链接")
            tv_save("tv_primary", None)
            tv_save("tv_remote", None)
            if tv_can_manage(principal, pending):
                tv_save("tv_pending", None)
            store.db.commit()
        return {"ok": True}

    @app.get("/api/tv/state")
    def tv_state(token: str):
        require_tv(token)
        with store.lock:
            snapshot = state_with_active()
            remote = tv_remote_record()
            snapshot["remote_command"] = (
                remote["command"]
                if store.playback_mode() == "tv" and remote["seq"] > remote["ack"]
                else None
            )
            return snapshot

    @app.post("/api/tv/replay-next")
    def tv_replay_next(body: Finish, token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            return schedule_replay(body.song_id, current_only=True)

    @app.get("/api/tv/preload")
    def tv_preload(token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() != "tv" or not store.current():
                return {"candidate": None}
            # A preview, never a claim: do not advance fair counts or song state.
            queue = store.fair_queue()
            if not queue:
                return {"candidate": None}
            song = queue[0]
            media = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song["id"],)
            ).fetchone()
            return {
                "candidate": {
                    "id": song["id"],
                    "media_file": media["media_file"],
                    "source_file": media["source_file"],
                    "media_url": signed_media(song),
                    "html_overlay": overlay_view(dict(media)),
                }
            }

    @app.post("/api/tv/next")
    def tv_next(token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            return next_impl()

    @app.post("/api/tv/finish")
    def tv_finish(body: Finish, token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            return finish_impl(body)

    @app.get("/api/songs/history")
    def guest_song_history(vc_guest: str | None = Cookie(default=None)):
        user = current_user(vc_guest)
        archive = archived_guest_event(vc_guest) if not user else None
        if not user and not archive:
            require_user(vc_guest)
        if archive:
            with events.lock:
                path = events.database_path(archive["id"])
                with sqlite3.connect(
                    f"file:{quote(str(path), safe='/')}?mode=ro", uri=True
                ) as db:
                    db.row_factory = sqlite3.Row
                    columns = {
                        column[1] for column in db.execute("PRAGMA table_info(songs)")
                    }
                    frozen = (
                        "archive_order" in columns
                        and db.execute(
                            "SELECT 1 FROM songs WHERE archive_order IS NOT NULL LIMIT 1"
                        ).fetchone()
                    )
                    order = (
                        "s.archive_order,s.id"
                        if frozen
                        else """CASE
                           WHEN s.status IN ('played','skipped') THEN 0
                           WHEN s.status IN ('playing','closed_interrupted') THEN 1
                           WHEN s.status='cancelled' THEN 3 ELSE 2 END,
                           s.played_at,s.requested_at,s.id"""
                    )
                    songs = [
                        dict(row)
                        for row in db.execute(
                            """SELECT s.id,s.bvid,s.page,s.title,s.uploader,
                                      s.status,s.source_type,s.source_id,
                                      s.prep_state,s.skip_subtitles,
                                      g.name AS nickname
                               FROM songs s JOIN guests g ON g.id=s.requester
                               ORDER BY """
                            + order
                        )
                    ]
            return {"songs": songs, "archived": True}
        with store.lock:
            order = store.close_song_order()
            records = store.rows(
                """SELECT s.id,s.bvid,s.page,s.title,s.uploader,s.status,
                          s.source_type,s.source_id,s.prep_state,
                          s.skip_subtitles,s.media_file,
                          g.name AS nickname
                   FROM songs s JOIN guests g ON g.id=s.requester"""
            )
            rank = {song_id: i for i, song_id in enumerate(order)}
            records.sort(key=lambda song: rank.get(song["id"], len(rank)))
            for song in records:
                song["downloadable"] = (
                    Store.downloadable(song)
                    and (store.media / Path(song["media_file"] or "").name).is_file()
                )
                song.pop("media_file", None)
            return {"songs": records, "archived": False}

    @app.get("/api/songs/played")
    def guest_played_songs(
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if admin_principal(vc_admin) is None:
            require_user(vc_guest)
        with store.lock:
            rows = store.rows(
                """SELECT s.id,s.bvid,s.source_type,s.source_id,s.title,s.duration,
                          s.uploader,s.played_at,s.page,s.prep_state,s.media_file,s.media_file IS NOT NULL AS has_media,
                          g.name AS nickname
                   FROM songs s JOIN guests g ON g.id=s.requester
                   WHERE s.status='played'
                   ORDER BY s.played_at DESC,s.id DESC"""
            )
            for song in rows:
                song["downloadable"] = (
                    Store.downloadable({**song, "status": "played"})
                    and (store.media / Path(song["media_file"] or "").name).is_file()
                )
                song.pop("media_file", None)
            return {"songs": rows}

    @app.get("/api/songs/{song_id}/detail")
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
                          s.prep_stage_at,s.prep_timing,s.skip_subtitles,s.priority_next,
                          s.played_at,s.media_file,s.media_file IS NOT NULL AS has_media,
                          s.lyric_choice!='' AS lyric_selected,
                          s.lyric_timing_mode,s.lyric_offset_ms,s.source_file,
                          g.id AS requester_id,g.name AS nickname
                   FROM songs s JOIN guests g ON g.id=s.requester
                   WHERE s.id=?""",
                (song_id,),
            ).fetchone()
            if row is None:
                bail(404, "歌曲不存在")
            song = dict(row)
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

    @app.post("/api/songs/{song_id}/cancel")
    def cancel_own_song(song_id: int, vc_guest: str | None = Cookie(default=None)):
        user = require_user(vc_guest)
        with store.lock:
            row = store.db.execute(
                "SELECT requester,status FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["requester"] != user["id"]:
                bail(403, "只能取消自己点的歌曲")
            try:
                cur = store.db.execute(
                    "UPDATE songs SET status='cancelled' WHERE id=? "
                    "AND status IN ('waiting','ready','rendering')",
                    (song_id,),
                )
                if cur.rowcount == 0:
                    store.db.rollback()
                    bail(409, "歌曲已经开始播放，无法取消")
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        stop_song_processing(song_id)
        return {"ok": True}

    @app.post("/api/admin/songs/{song_id}/cancel")
    def cancel(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            try:
                cur = store.db.execute(
                    "UPDATE songs SET status='cancelled' WHERE id=? AND status IN ('waiting','ready','rendering')",
                    (song_id,),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
            if cur.rowcount == 0:
                bail(409, "只能取消尚未播放的歌曲")
        stop_song_processing(song_id)
        return {"ok": True}

    @app.get("/api/songs/{song_id}/download")
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
                "SELECT title,status,prep_state,media_file FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if not Store.downloadable(dict(row)):
                bail(409, "视频尚未准备好")
            file = store.media / Path(row["media_file"]).name
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

    @app.get("/api/songs/{song_id}/download/source")
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
                "SELECT title,status,source_file FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if (
                row["status"] not in ("ready", "playing", "played", "skipped")
                or not row["source_file"]
            ):
                bail(409, "原片尚未准备好")
            file = store.media / Path(row["source_file"]).name
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

    @app.get("/media/{song_id}/source")
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
            if not row or not row["source_file"]:
                bail(404, "原视频不存在")
            song = dict(row)
            # Refuse source if it would exceed the admin's selected budget.
            if not overlay_view(song):
                bail(409, "原片模式不可用，请使用烧录版")
            path = store.media / Path(song["source_file"]).name
            if (
                path.stat().st_size * 8
                > float(song["duration"] or 0)
                * store.settings()["max_bandwidth_mbps"]
                * 800_000
            ):
                path = overlay_archive(overlay_bundle(song)) / "tv-source.mp4"
            if not path.is_file():
                bail(404, "原视频文件缺失")
            return pinned_media_response(
                path,
                media_type=MEDIA_MIMES.get(path.suffix, "video/mp4"),
                content_disposition_type="inline",
            )

    @app.get("/api/tv/subtitles/{song_id}")
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
            folder = overlay_bundle(dict(row))
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

    @app.get("/media/{song_id}")
    def serve_media(song_id: int, token: str = ""):
        if (
            read_signed(token, "media") != f"{events.active_id}:{song_id}"
            or not events.active_id
        ):
            bail(403, "媒体链接无效或已过期")
        # Share AutoPrep's Store mutex: do not serve an uncommitted media path.
        with store.lock:
            row = store.db.execute(
                "SELECT media_file FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row or not row[0]:
                bail(404, "媒体不存在")
            name = Path(row[0]).name
            file = store.media / name
            if not file.is_file():
                bail(404, "媒体文件缺失")
            return pinned_media_response(
                file,
                media_type=MEDIA_MIMES.get(file.suffix, "application/octet-stream"),
                filename=name,
                content_disposition_type="inline",
            )

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_page():
        return FileResponse(STATIC / "index.html")

    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
    return app
