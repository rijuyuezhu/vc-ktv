"""Self-hosted BV-first karaoke with optional local subtitle generation."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
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
from .events import CurrentStore, Events
from .karaoke import render_video, write_ass
from .lyric_selection import search_lyric_candidates
from .lyric_sources import vcpedia_lyric_candidates
from .subtitle_styles import StylePreset
from .textgrid_import import convert_textgrid

BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")
MAX_UPLOAD = 512 * 1024 * 1024
MAX_ACTIVE_SONGS = 1000
MAX_USER_SONGS = 10
MAX_LRC_UPLOAD = 128 * 1024
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


class Preview(BaseModel):
    bvid: str
    page: int = Field(default=1, ge=1, le=200)


class RequestSong(Preview):
    nickname: str = Field(default="", max_length=24)


class GuestLyricChoice(BaseModel):
    text: str = Field(min_length=10, max_length=12_000)
    ticket: str = Field(min_length=10, max_length=256)
    source: str = Field(min_length=2, max_length=20)


class Login(BaseModel):
    password: str
    username: str | None = None


class EventAdminCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=24)
    password: str = Field(min_length=6, max_length=72)


class EventAdminPassword(BaseModel):
    password: str = Field(min_length=6, max_length=72)


class PlaybackSettings(BaseModel):
    queue_policy: Literal["fair", "fcfs"]
    allow_unsubtitled: bool
    # Old clients that omit the new field must not reset the chosen preset.
    subtitle_style: StylePreset | None = None
    video_quality: Literal[720, 480] | None = None
    # Optional to preserve older admin clients' current per-event setting.
    compact_video: bool | None = None
    orders_paused: bool | None = None


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
    mode: Literal["browser", "manual"]


class Lyrics(BaseModel):
    text: str = Field(max_length=30000)


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
        if not 0 < duration <= 3600:
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


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.media = root / "media"
        self.media.mkdir(exist_ok=True)
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
        ):
            if column not in columns:
                self.db.execute(f"ALTER TABLE songs ADD COLUMN {column} {ddl}")
        self.db.commit()

    def rows(self, sql, args=()):
        return [dict(x) for x in self.db.execute(sql, args).fetchall()]

    def current(self):
        rows = self.rows(
            "SELECT * FROM songs WHERE status='playing' ORDER BY id LIMIT 1"
        )
        return rows[0] if rows else None

    def songs(self):
        rows = self.rows("""
            SELECT s.id,s.bvid,s.page,s.title,s.uploader,s.cover,s.duration,
                   s.requested_at,s.status,s.media_file IS NOT NULL AS has_media,s.render_error,
                   s.prep_state,s.prep_error,s.prep_attempts,s.prep_retry_at,
                   s.skip_subtitles,s.prep_stage,s.prep_stage_at,s.priority_next,s.replay_of,
                   s.lyric_choice != '' AS lyric_selected,s.lyric_source,
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
                "WHERE name IN ('queue_policy','allow_unsubtitled','subtitle_style','video_quality','compact_video','orders_paused')"
            )
        }
        return {
            "queue_policy": values.get("queue_policy", "fair"),
            "allow_unsubtitled": values.get("allow_unsubtitled", "0") == "1",
            "subtitle_style": values.get("subtitle_style", "classic"),
            "video_quality": int(values.get("video_quality", "720")),
            "compact_video": values.get("compact_video", "0") == "1",
            "orders_paused": values.get("orders_paused", "0") == "1",
        }

    def playback_mode(self):
        row = self.db.execute(
            "SELECT value FROM room_state WHERE name='playback_mode'"
        ).fetchone()
        return row[0] if row and row[0] == "manual" else "browser"

    def save_settings(
        self,
        queue_policy: str,
        allow_unsubtitled: bool,
        subtitle_style: StylePreset | None = None,
        video_quality: Literal[720, 480] | None = None,
        compact_video: bool | None = None,
        orders_paused: bool | None = None,
    ):
        with self.lock:
            for key, value in (
                ("queue_policy", queue_policy),
                ("allow_unsubtitled", "1" if allow_unsubtitled else "0"),
            ):
                self.db.execute(
                    "INSERT INTO room_state(name,value) VALUES(?,?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            if subtitle_style is not None:
                self.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('subtitle_style',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (subtitle_style,),
                )
            if video_quality is not None:
                self.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('video_quality',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (str(video_quality),),
                )
            if compact_video is not None:
                self.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('compact_video',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    ("1" if compact_video else "0",),
                )
            if orders_paused is not None:
                self.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('orders_paused',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    ("1" if orders_paused else "0",),
                )
            self.db.commit()
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
            song["status"] in ("ready", "playing", "played")
            and song["prep_state"] in ("done", "manual", "original")
            and bool(song.get("has_media") or song.get("media_file"))
        )

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
    if duration <= 0 or duration > 3600:
        bail(400, "MVP 仅支持 1 秒到 60 分钟的视频")
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
        "cover": d.get("pic", ""),
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
    if auto_prepare is None:
        auto_prepare = os.getenv("VC_KTV_AUTOPREPARE", "1") != "0"
    worker = AutoPrep(events.active() if events.active_id else store, probe_media)
    running = False

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal running
        running = True
        if auto_prepare and events.active_id:
            worker.store = (
                events.active()
            )  # Also handle events created before lifespan.
            worker.start()
        try:
            yield
        finally:
            running = False
            if auto_prepare and worker.worker:
                worker.stop()

    app = FastAPI(title="VC KTV MVP", version="0.5.0", lifespan=lifespan)
    app.state.autoprep = worker
    app.state.store = store
    app.state.events = events
    app.state.lookup = bilibili_lookup
    app.state.lyrics_lookup = vcpedia_find
    app.state.candidate_search = search_lyric_candidates
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

    def check_room_capacity():
        count = store.db.execute(
            "SELECT COUNT(*) FROM songs WHERE status IN ('waiting','rendering','ready','playing')"
        ).fetchone()[0]
        if count >= MAX_ACTIVE_SONGS:
            bail(409, "本场待处理歌曲已满，请稍后再点歌")

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
        return info

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

    @app.get("/api/entry")
    def entry(
        vc_join: str | None = Cookie(default=None),
        vc_guest: str | None = Cookie(default=None),
    ):
        event = events.metadata()
        if not event:
            return {
                "event": None,
                "joined": False,
                "authenticated": False,
                "username": None,
            }
        user = current_user(vc_guest)
        joined = bool(user or read_signed(vc_join, "entry") == events.active_id)
        return {
            "event": {"id": event["id"], "name": event["name"]},
            "joined": joined,
            "authenticated": bool(user),
            "username": user["name"] if user else None,
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
                body.compact_video,
                body.orders_paused,
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

    @app.get("/api/admin/events/{event_id}/songs.csv")
    def export_event_songs(event_id: str, vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        if principal["role"] == "event" and event_id != events.active_id:
            bail(403, "无权查看其他场次历史")
        with events.lock:
            path = events.database_path(event_id)
            with sqlite3.connect(
                f"file:{quote(str(path), safe='/')}?mode=ro", uri=True
            ) as db:
                songs = db.execute(
                    """SELECT s.bvid,s.page,s.title,g.name,s.requested_at,
                              s.status,s.played_at
                       FROM songs s JOIN guests g ON g.id=s.requester
                       ORDER BY s.requested_at,s.id"""
                ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            (
                "order",
                "bvid",
                "page",
                "title",
                "requester",
                "requested_at",
                "status",
                "played_at",
            )
        )
        for index, song in enumerate(songs, 1):
            # Avoid spreadsheet formula execution for untrusted song titles/usernames.
            fields = list(song)
            for field in (2, 3):
                if fields[field] and str(fields[field])[0] in "=+-@\t\r":
                    fields[field] = "'" + fields[field]
            writer.writerow((index, *fields))
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="vc-ktv-{event_id}-songs.csv"'
            },
        )

    @app.post("/api/admin/events")
    def create_event(body: EventCreate, vc_admin: str | None = Cookie(default=None)):
        nonlocal worker
        require_global_admin(vc_admin)
        with events.lock:
            entry = events.create(body.name, body.code)
            worker = AutoPrep(events.active(), probe_media)
            app.state.autoprep = worker
            if running and auto_prepare:
                worker.start()
        return entry

    @app.post("/api/admin/events/close")
    def close_event(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with events.lock:
            if not events.active_id:
                bail(409, "没有活动场次")
            if store.current():
                bail(409, "请先结束正在播放的歌曲")
            if worker.busy.is_set() or rendering.is_set():
                bail(409, "请等待当前媒体制作任务完成")
            events.transitioning = True
            try:
                # Block new readers before stopping the worker. Bound the wait:
                # an outstanding slow HTTP request must not freeze close.
                if not events.idle.wait_for(lambda: events.inflight == 0, timeout=10):
                    bail(409, "仍有请求正在处理，请稍后重试")
                # The worker is pinned to this Store, and its store lock makes
                # the idle/claim check atomic with the stop request.
                with events.active().lock:
                    if worker.busy.is_set():
                        bail(409, "请等待当前媒体制作任务完成")
                    worker.stop_signal.set()
                if not worker.stop():
                    bail(409, "后台任务尚未退出，请稍后再试")
                return events.close()
            finally:
                events.transitioning = False

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": "0.5.0", "auto_prepare": auto_prepare}

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
        return {
            "id": cur.lastrowid,
            "status": "waiting",
            "title": info["title"],
            "lyric_query": title_key(info["title"]),
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

    def save_guest_lyrics(song_id: int, requester: str, lyrics: str, source: str):
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
            store.db.execute(
                "UPDATE songs SET lyric_choice=?,lyric_source=?,prep_state=?,"
                "skip_subtitles=0,prep_stage='queued',prep_error='' WHERE id=?",
                (lyrics, source, next_state, song_id),
            )
            store.db.commit()
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
            store.db.execute(
                "UPDATE songs SET skip_subtitles=1,lyric_choice='',lyric_source='',"
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
        term = query.strip() if query else title_key(song["title"])[:60].strip()
        if not 1 <= len(term) <= 60:
            bail(400, "请使用 1–60 字的歌名关键词")
        key = (song["requester"], song_id)
        recent = [t for t in lyric_search_times.get(key, []) if t > time_now() - 300]
        if len(recent) >= 30:
            bail(429, "搜索太频繁，请五分钟后重试")
        recent.append(time_now())
        lyric_search_times[key] = recent
        candidates = (
            await app.state.candidate_search(term)
            if phase == "all"
            else await search_lyric_candidates(term, phase)
        )
        for candidate in candidates:
            digest = hashlib.sha256(candidate["text"].encode()).hexdigest()
            candidate["ticket"] = sign(
                "lyric",
                f"{events.active_id}:{song_id}:{candidate['source']}:{digest}",
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
        return save_guest_lyrics(song_id, song["requester"], body.text, body.source)

    @app.post("/api/songs/{song_id}/lyrics/upload")
    async def guest_upload_lrc(
        song_id: int,
        file: Annotated[UploadFile, File()],
        vc_guest: str | None = Cookie(default=None),
    ):
        song = own_song(song_id, vc_guest)
        if not (file.filename or "").lower().endswith(".lrc"):
            bail(400, "请上传 .lrc 歌词文件")
        raw = await file.read(MAX_LRC_UPLOAD + 1)
        await file.close()
        if len(raw) > MAX_LRC_UPLOAD:
            bail(413, "LRC 文件最大 128 KiB")
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
        if len(lines) < 2 or not 10 <= len(lyrics) <= 12_000:
            bail(400, "LRC 需要至少两行带时间标签的有效歌词，总长度不超过 12000 字符")
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
                bail(409, "本场管理员用户名已存在")
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
            updated = store.db.execute(
                "UPDATE event_admins SET password_hash=?,revision=revision+1 WHERE id=?",
                (hashed, account_id),
            )
            store.db.commit()
        if not updated.rowcount:
            bail(404, "管理员账号不存在")
        return {"ok": True}

    @app.delete("/api/admin/accounts/{account_id}")
    def revoke_event_admin(
        account_id: str, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        with store.lock:
            deleted = store.db.execute(
                "DELETE FROM event_admins WHERE id=?", (account_id,)
            )
            store.db.commit()
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
        return {**store.state(), "active": active_view()}

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
            store.db.execute(
                "UPDATE songs SET prep_state='downloaded',prep_error='',"
                "skip_subtitles=0,prep_stage='queued',prep_stage_at=? WHERE id=?",
                (time_now(), song_id),
            )
            store.db.commit()
        return {"ok": True, "id": song_id, "status": "downloaded"}

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
            store.db.execute(
                "UPDATE songs SET media_file=source_file,prep_state='original',"
                "skip_subtitles=1,prep_stage='skipped',prep_stage_at=?,"
                "prep_error='管理员已选择播放原片' WHERE id=?",
                (time_now(), song_id),
            )
            store.db.commit()
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
                    if size > MAX_UPLOAD:
                        bail(413, "文件太大，单首最大 512 MiB")
                    out.write(block)
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
                store.db.execute(
                    "UPDATE songs SET status='ready',media_file=?,source_file=?,duration=?,render_error='',prep_state='manual',prep_error='' WHERE id=?",
                    (target.name, target.name, round(actual_duration), song_id),
                )
                store.db.commit()
            for old in set(filter(None, (row["media_file"], row["source_file"]))):
                if old != target.name:
                    (store.media / old).unlink(missing_ok=True)
        except BaseException:
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
            store.db.execute(
                "UPDATE songs SET lyrics=? WHERE id=?", (body.text, song_id)
            )
            store.db.commit()
        return {"ok": True}

    @app.get("/api/admin/songs/{song_id}/lyrics")
    def get_lyrics(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
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
        try:
            render_video(source, lyrics, destination, duration, lead)
            with target_store.lock:
                row = target_store.db.execute(
                    "SELECT media_file,status FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if row and row["status"] == "rendering":
                    target_store.db.execute(
                        "UPDATE songs SET status='ready',media_file=?,render_error='' WHERE id=?",
                        (destination.name, song_id),
                    )
                    target_store.db.commit()
                    if row["media_file"] != source.name:
                        (target_store.media / row["media_file"]).unlink(missing_ok=True)
                else:
                    destination.unlink(missing_ok=True)
        except Exception as error:
            destination.unlink(missing_ok=True)
            with target_store.lock:
                target_store.db.execute(
                    "UPDATE songs SET status='ready',render_error=? WHERE id=? AND status='rendering'",
                    (f"制作失败：{type(error).__name__}", song_id),
                )
                target_store.db.commit()
        finally:
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
        content = await file.read(2 * 1024 * 1024 + 1)
        await file.close()
        if len(content) > 2 * 1024 * 1024:
            bail(413, "TextGrid 文件最多 2 MiB")
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
                store.db.execute(
                    "UPDATE songs SET lyrics=? WHERE id=?", (proposed, song_id)
                )
                store.db.commit()
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
                store.db.execute(
                    "UPDATE songs SET status='rendering',render_error='' WHERE id=?",
                    (song_id,),
                )
                store.db.commit()
                rendering.set()
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
                return {"id": song_id, "status": "rendering", "lead": body.lead}
        except BaseException:
            render_slot.release()
            raise

    def next_impl():
        with store.lock:
            if not store.current():
                queue = store.fair_queue(persist=True)
                if queue:
                    song = queue[0]
                    # Store the exemption on the song: once priority_next is
                    # cleared, finish/skip must still know this was an admin turn.
                    store.db.execute(
                        "UPDATE songs SET status='playing',priority_next=0,fair_exempt=? WHERE id=?",
                        (int(bool(song["priority_next"])), song["id"]),
                    )
                    store.db.commit()
            return {"active": active_view()}

    def finish_impl(body: Finish):
        with store.lock:
            song = store.current()
            if not song or song["id"] != body.song_id:
                bail(409, "当前播放已变化")
            completed = body.reason != "skip"
            store.db.execute(
                "UPDATE songs SET status=?,played_at=? WHERE id=?",
                ("played" if completed else "skipped", time_now(), body.song_id),
            )
            if not song["fair_exempt"]:
                if completed:
                    store.db.execute(
                        "UPDATE guests SET plays=plays+1,seconds=seconds+? WHERE id=?",
                        (song["duration"], song["requester"]),
                    )
                else:
                    # A skipped ordinary turn consumes only virtual song credit.
                    store.db.execute(
                        "UPDATE guests SET fair_offset=fair_offset+1 WHERE id=?",
                        (song["requester"],),
                    )
                store.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('last_requester',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (song["requester"],),
                )
            store.db.commit()
            return {
                "ok": True,
                "finished": body.song_id,
                "next_ready": len(store.fair_queue()),
            }

    @app.put("/api/admin/playback-mode")
    def change_playback_mode(
        body: PlaybackMode, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() != body.mode and store.current():
                bail(409, "请先唱完或跳过当前歌曲，再切换播放模式")
            store.db.execute(
                "INSERT INTO room_state(name,value) VALUES('playback_mode',?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (body.mode,),
            )
            store.db.commit()
        return {"mode": body.mode}

    @app.get("/api/admin/played")
    def recently_played(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            return {
                "songs": store.rows(
                    """SELECT s.id,s.bvid,s.title,s.duration,s.page,s.played_at,s.replay_of,
                          g.name AS nickname
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
            store.db.execute("UPDATE songs SET priority_next=0 WHERE priority_next=1")
            store.db.execute("UPDATE songs SET priority_next=1 WHERE id=?", (song_id,))
            store.db.commit()
        return {"ok": True, "id": song_id, "priority_next": True}

    @app.post("/api/admin/songs/{song_id}/replay")
    def replay_song(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if (
                row["status"] != "played"
                or row["prep_state"] not in ("done", "manual", "original")
                or not row["media_file"]
            ):
                bail(409, "只能重唱本场已唱完且有成片的歌曲")
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
                store.db.execute(
                    "UPDATE songs SET priority_next=0 WHERE priority_next=1"
                )
                cur = store.db.execute(
                    """INSERT INTO songs(
                        bvid,page,title,uploader,cover,duration,requester,requested_at,
                        status,lyrics,lyric_choice,lyric_source,prep_state,
                        skip_subtitles,priority_next,replay_of
                    ) VALUES(?,?,?,?,?,?,?,?,'ready',?,?,?,?,?,1,?)""",
                    (
                        row["bvid"],
                        row["page"],
                        row["title"],
                        row["uploader"],
                        row["cover"],
                        row["duration"],
                        row["requester"],
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
                        song_id,
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
                    "UPDATE songs SET media_file=?,source_file=? WHERE id=?",
                    (new_media, new_source, new_id),
                )
                store.db.commit()
            except BaseException:
                store.db.rollback()
                for link in links:
                    link.unlink(missing_ok=True)
                raise
        return {"id": new_id, "status": "ready", "priority_next": True}

    @app.post("/api/admin/next")
    def next_song(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return next_impl()

    @app.post("/api/admin/finish")
    def finish_song(body: Finish, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return finish_impl(body)

    def require_tv(token: str):
        identity = read_signed(token, "tv")
        if not identity or not events.active_id:
            bail(401, "电视端链接已过期，请管理员重新打开")
        if identity in (events.active_id, f"{events.active_id}:global"):
            return  # Existing global-admin TV links remain valid until expiry.
        parts = identity.split(":")
        if len(parts) != 4 or parts[:2] != [events.active_id, "event"]:
            bail(401, "电视端链接已过期，请管理员重新打开")
        with store.lock:
            account = store.db.execute(
                "SELECT revision FROM event_admins WHERE id=?", (parts[2],)
            ).fetchone()
        if not account or str(account["revision"]) != parts[3]:
            bail(401, "电视端链接已过期，请管理员重新打开")

    @app.get("/api/admin/tv-link")
    def tv_link(vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        events.active()
        identity = (
            f"{events.active_id}:global"
            if principal["role"] == "global"
            else f"{events.active_id}:event:{principal['id']}:{principal['revision']}"
        )
        return {"url": "/tv.html?token=" + sign("tv", identity, 8 * 3600)}

    @app.get("/api/tv/state")
    def tv_state(token: str):
        require_tv(token)
        return {**store.state(), "active": active_view()}

    @app.get("/api/tv/preload")
    def tv_preload(token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() != "browser" or not store.current():
                return {"candidate": None}
            # A preview, never a claim: do not advance fair counts or song state.
            queue = store.fair_queue()
            if not queue:
                return {"candidate": None}
            song = queue[0]
            media = store.db.execute(
                "SELECT media_file FROM songs WHERE id=?", (song["id"],)
            ).fetchone()
            return {
                "candidate": {
                    "id": song["id"],
                    "media_file": media[0],
                    "media_url": signed_media(song),
                }
            }

    @app.post("/api/tv/next")
    def tv_next(token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() == "manual":
                bail(409, "当前使用手动投屏，请在管理后台操作")
            return next_impl()

    @app.post("/api/tv/finish")
    def tv_finish(body: Finish, token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() == "manual":
                bail(409, "当前使用手动投屏，请在管理后台操作")
            return finish_impl(body)

    @app.get("/api/songs/played")
    def guest_played_songs(
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if admin_principal(vc_admin) is None:
            require_user(vc_guest)
        with store.lock:
            rows = store.rows(
                """SELECT s.id,s.bvid,s.title,s.duration,s.played_at,s.page,
                          s.prep_state,s.media_file,s.media_file IS NOT NULL AS has_media,
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
                """SELECT s.id,s.title,s.bvid,s.page,s.uploader,s.duration,
                          s.status,s.prep_state,s.prep_error,s.prep_stage,
                          s.prep_stage_at,s.prep_timing,s.skip_subtitles,s.priority_next,
                          s.played_at,s.media_file IS NOT NULL AS has_media,
                          s.lyric_choice!='' AS lyric_selected,
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
            song["downloadable"] = Store.downloadable(song)
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
            cur = store.db.execute(
                "UPDATE songs SET status='cancelled' WHERE id=? "
                "AND status IN ('waiting','ready','rendering')",
                (song_id,),
            )
            if cur.rowcount == 0:
                bail(409, "歌曲已经开始播放，无法取消")
            store.db.commit()
        return {"ok": True}

    @app.post("/api/admin/songs/{song_id}/cancel")
    def cancel(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            cur = store.db.execute(
                "UPDATE songs SET status='cancelled' WHERE id=? AND status IN ('waiting','ready','rendering')",
                (song_id,),
            )
            store.db.commit()
            if cur.rowcount == 0:
                bail(409, "只能取消尚未播放的歌曲")
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
        return FileResponse(
            file,
            media_type=MEDIA_MIMES.get(file.suffix, "application/octet-stream"),
            filename=f"vc-ktv-{song_id}-{title}{file.suffix}",
            content_disposition_type="attachment",
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/media/{song_id}")
    def serve_media(song_id: int, token: str = ""):
        if (
            read_signed(token, "media") != f"{events.active_id}:{song_id}"
            or not events.active_id
        ):
            bail(403, "媒体链接无效或已过期")
        row = store.db.execute(
            "SELECT media_file FROM songs WHERE id=?", (song_id,)
        ).fetchone()
        if not row or not row[0]:
            bail(404, "媒体不存在")
        name = Path(row[0]).name
        file = store.media / name
        if not file.is_file():
            bail(404, "媒体文件缺失")
        return FileResponse(
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
