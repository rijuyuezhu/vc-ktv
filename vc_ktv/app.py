"""Self-hosted BV-first karaoke with optional local subtitle generation."""

from __future__ import annotations

import hashlib
import hmac
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
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import Cookie, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .autoprep import AutoPrep, title_key
from .karaoke import render_video, write_ass
from .lyric_selection import search_lyric_candidates
from .lyric_sources import vcpedia_lyric_candidates
from .textgrid_import import convert_textgrid

BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")
MAX_UPLOAD = 512 * 1024 * 1024
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


class Finish(BaseModel):
    song_id: int
    reason: str = ""


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
        """)
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
        self.db.commit()

    def rows(self, sql, args=()):
        return [dict(x) for x in self.db.execute(sql, args).fetchall()]

    def current(self):
        rows = self.rows(
            "SELECT * FROM songs WHERE status='playing' ORDER BY id LIMIT 1"
        )
        return rows[0] if rows else None

    def songs(self):
        return self.rows("""
            SELECT s.id,s.bvid,s.page,s.title,s.uploader,s.cover,s.duration,
                   s.requested_at,s.status,s.media_file IS NOT NULL AS has_media,s.render_error,
                   s.prep_state,s.prep_error,s.prep_attempts,s.prep_retry_at,
                   s.lyric_choice != '' AS lyric_selected,s.lyric_source,
                   g.name AS nickname,g.id AS requester_id,s.lyrics != '' AS has_lyrics
            FROM songs s JOIN guests g ON g.id=s.requester
            WHERE s.status IN ('waiting','rendering','ready','playing')
            ORDER BY s.requested_at,s.id
        """)

    def fair_queue(self):
        """One ready track per guest per turn; least-played users first."""
        pool = self.songs()
        ready = [s for s in pool if s["status"] == "ready"]
        users = {x["id"]: x for x in self.rows("SELECT * FROM guests")}
        # Simulate future turns so the displayed order reflects selection policy.
        tally = {k: (u["plays"], u["seconds"]) for k, u in users.items()}
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
                    tally[s["requester_id"]][0],
                    s["requester_id"] == previous,
                    tally[s["requester_id"]][1],
                    s["requested_at"],
                    s["id"],
                ),
            )
            output.append(chosen)
            a, b = tally[chosen["requester_id"]]
            tally[chosen["requester_id"]] = (a + 1, b + chosen["duration"])
            previous = chosen["requester_id"]
            ready.remove(chosen)
        return output

    def state(self):
        with self.lock:
            return {
                "current": next(
                    (s for s in self.songs() if s["status"] == "playing"), None
                ),
                "queue": self.fair_queue(),
                "waiting": [
                    s for s in self.songs() if s["status"] in ("waiting", "rendering")
                ],
                "guests": self.rows(
                    "SELECT name,plays,seconds FROM guests ORDER BY name"
                ),
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
    store = Store(root)
    if auto_prepare is None:
        auto_prepare = os.getenv("VC_KTV_AUTOPREPARE", "1") != "0"
    worker = AutoPrep(store, probe_media)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if auto_prepare:
            worker.start()
        try:
            yield
        finally:
            if auto_prepare:
                worker.stop()

    app = FastAPI(title="VC KTV MVP", version="0.5.0", lifespan=lifespan)
    app.state.autoprep = worker
    app.state.store = store
    app.state.lookup = bilibili_lookup
    app.state.lyrics_lookup = vcpedia_find
    app.state.candidate_search = search_lyric_candidates
    lyric_search_times = {}
    failed_login = {}
    app.state.probe = probe_media
    render_slot = threading.BoundedSemaphore(1)

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

    def require_admin(token):
        if read_signed(token, "admin") != "yes":
            bail(401, "需要管理员登录")

    def signed_media(song: dict) -> str:
        return f"/media/{song['id']}?token={quote(sign('media', str(song['id']), 8 * 3600))}"

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
        return await call_next(request)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": "0.5.0", "auto_prepare": auto_prepare}

    @app.get("/api/state")
    def state(vc_guest: str | None = Cookie(default=None)):
        snapshot = store.state()
        guest = read_signed(vc_guest, "guest")
        for song in [*snapshot["queue"], *snapshot["waiting"]]:
            song["mine"] = bool(guest and song["requester_id"] == guest)
        if snapshot["current"]:
            snapshot["current"]["mine"] = bool(
                guest and snapshot["current"]["requester_id"] == guest
            )
        return snapshot

    @app.get("/api/preview")
    async def preview(bvid: str, page: int = 1):
        return await app.state.lookup(get_bvid(bvid), page)

    @app.get("/api/lyrics/search")
    async def lyrics_search(query: str):
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
        info = await app.state.lookup(get_bvid(body.bvid), body.page)
        guest = read_signed(vc_guest, "guest") or secrets.token_hex(12)
        nickname = body.nickname.strip() or f"听众-{guest[:4].upper()}"
        with store.lock:
            # Prevent a single person from flooding an active room.
            n = store.db.execute(
                "SELECT COUNT(*) FROM songs WHERE requester=? AND status IN ('ready','waiting','rendering','playing')",
                (guest,),
            ).fetchone()[0]
            if n >= 3:
                bail(409, "每人最多保留三首待唱歌曲")
            store.db.execute(
                "INSERT INTO guests(id,name) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (guest, nickname),
            )
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
        response.set_cookie(
            "vc_guest",
            sign("guest", guest, 90 * 86400),
            max_age=90 * 86400,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"id": cur.lastrowid, "status": "waiting", "title": info["title"]}

    def own_song(song_id: int, guest_token: str | None):
        guest = read_signed(guest_token, "guest")
        if not guest:
            bail(401, "请从点歌时使用的浏览器操作")
        with store.lock:
            song = store.db.execute(
                "SELECT * FROM songs WHERE id=? AND requester=?", (song_id, guest)
            ).fetchone()
        if not song:
            bail(404, "找不到你提交的歌曲")
        return dict(song)

    @app.get("/api/songs/{song_id}/lyrics/candidates")
    async def guest_lyric_candidates(
        song_id: int,
        query: str = "",
        vc_guest: str | None = Cookie(default=None),
    ):
        song = own_song(song_id, vc_guest)
        if song["status"] not in ("waiting", "ready"):
            bail(409, "这首歌已经开始播放或结束")
        term = query.strip() if query else title_key(song["title"])[:60].strip()
        if not 2 <= len(term) <= 60:
            bail(400, "请使用 2–60 字的歌名关键词")
        key = (song["requester"], song_id)
        recent = [t for t in lyric_search_times.get(key, []) if t > time_now() - 300]
        if len(recent) >= 12:
            bail(429, "搜索太频繁，请五分钟后重试")
        recent.append(time_now())
        lyric_search_times[key] = recent
        candidates = await app.state.candidate_search(term)
        for candidate in candidates:
            digest = hashlib.sha256(candidate["text"].encode()).hexdigest()
            candidate["ticket"] = sign(
                "lyric", f"{song_id}:{candidate['source']}:{digest}", 30 * 60
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
        if read_signed(body.ticket, "lyric") != f"{song_id}:{body.source}:{digest}":
            bail(403, "歌词候选已失效，请重新搜索")
        if body.source not in ("VCPedia", "LRCLIB"):
            bail(400, "歌词来源不受支持")
        if len(body.text.splitlines()) < 2:
            bail(400, "歌词候选无效")
        with store.lock:
            current = store.db.execute(
                "SELECT * FROM songs WHERE id=? AND requester=?",
                (song_id, song["requester"]),
            ).fetchone()
            if not current or current["status"] not in ("waiting", "ready"):
                bail(409, "歌曲已经开始播放或结束，不能切换歌词")
            if current["prep_state"] == "aligning":
                bail(409, "字幕正在制作中，暂不能更换歌词")
            next_state = (
                "downloaded" if current["status"] == "ready" else current["prep_state"]
            )
            store.db.execute(
                "UPDATE songs SET lyric_choice=?,lyric_source=?,prep_state=?,prep_error='' "
                "WHERE id=?",
                (body.text, body.source, next_state, song_id),
            )
            store.db.commit()
        return {"ok": True, "id": song_id, "status": next_state}

    @app.post("/api/admin/login")
    def login(body: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "unknown"
        recent = [t for t in failed_login.get(ip, []) if t > time_now() - 300]
        if len(recent) >= 10:
            bail(429, "登录尝试过于频繁，请稍后重试")
        if not hmac.compare_digest(body.password, password):
            recent.append(time_now())
            failed_login[ip] = recent
            bail(401, "密码错误")
        failed_login.pop(ip, None)
        response.set_cookie(
            "vc_admin",
            sign("admin", "yes", 7 * 86400),
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
        return {**store.state(), "active": active_view()}

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
        song_id: int, source: Path, lyrics: str, duration: float, lead: float
    ):
        destination = store.media / f"{song_id}-ktv-{secrets.token_hex(8)}.mp4"
        try:
            render_video(source, lyrics, destination, duration, lead)
            with store.lock:
                row = store.db.execute(
                    "SELECT media_file,status FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if row and row["status"] == "rendering":
                    store.db.execute(
                        "UPDATE songs SET status='ready',media_file=?,render_error='' WHERE id=?",
                        (destination.name, song_id),
                    )
                    store.db.commit()
                    if row["media_file"] != source.name:
                        (store.media / row["media_file"]).unlink(missing_ok=True)
                else:
                    destination.unlink(missing_ok=True)
        except Exception as error:
            destination.unlink(missing_ok=True)
            with store.lock:
                store.db.execute(
                    "UPDATE songs SET status='ready',render_error=? WHERE id=? AND status='rendering'",
                    (f"制作失败：{type(error).__name__}", song_id),
                )
                store.db.commit()
        finally:
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
                worker = threading.Thread(
                    target=render_worker,
                    args=(song_id, source, row["lyrics"], row["duration"], body.lead),
                    daemon=True,
                )
                worker.start()
                return {"id": song_id, "status": "rendering", "lead": body.lead}
        except BaseException:
            render_slot.release()
            raise

    def next_impl():
        with store.lock:
            if not store.current():
                queue = store.fair_queue()
                if queue:
                    song = queue[0]
                    store.db.execute(
                        "UPDATE songs SET status='playing' WHERE id=?", (song["id"],)
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
            if completed:
                store.db.execute(
                    "UPDATE guests SET plays=plays+1,seconds=seconds+? WHERE id=?",
                    (song["duration"], song["requester"]),
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

    @app.post("/api/admin/next")
    def next_song(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return next_impl()

    @app.post("/api/admin/finish")
    def finish_song(body: Finish, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return finish_impl(body)

    def require_tv(token: str):
        if read_signed(token, "tv") != "yes":
            bail(401, "电视端链接已过期，请管理员重新打开")

    @app.get("/api/admin/tv-link")
    def tv_link(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return {"url": "/tv.html?token=" + sign("tv", "yes", 8 * 3600)}

    @app.get("/api/tv/state")
    def tv_state(token: str):
        require_tv(token)
        return {**store.state(), "active": active_view()}

    @app.post("/api/tv/next")
    def tv_next(token: str):
        require_tv(token)
        return next_impl()

    @app.post("/api/tv/finish")
    def tv_finish(body: Finish, token: str):
        require_tv(token)
        return finish_impl(body)

    @app.post("/api/admin/songs/{song_id}/cancel")
    def cancel(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            cur = store.db.execute(
                "UPDATE songs SET status='cancelled' WHERE id=? AND status IN ('waiting','ready')",
                (song_id,),
            )
            store.db.commit()
            if cur.rowcount == 0:
                bail(409, "只能取消尚未播放的歌曲")
        return {"ok": True}

    @app.get("/media/{song_id}")
    def serve_media(song_id: int, token: str = ""):
        if read_signed(token, "media") != str(song_id):
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

    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
    return app
