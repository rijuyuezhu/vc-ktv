"""Small bounded cover cache shared by live orders and archive backfill."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx

MAX_DOWNLOAD = 4 * 1024 * 1024
MAX_JPEG = 256 * 1024
MAX_SIDE = 720
MAX_SOURCE_SIDE = 8192
MAX_SOURCE_PIXELS = 24_000_000
EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

_LOG = logging.getLogger(__name__)


def cover_file(directory: Path, song_id: int) -> Path | None:
    if song_id < 1 or directory.is_symlink():
        return None
    return next(
        (
            p
            for ext in EXTENSIONS
            if (p := directory / f"{song_id}{ext}").is_file() and not p.is_symlink()
        ),
        None,
    )


def _cover_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme == "https"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and (
                re.fullmatch(r"i\d+\.hdslb\.com", host)
                or re.fullmatch(r"p\d+\.music\.126\.net", host)
            )
        ):
            return host
    except ValueError:
        pass
    return ""


def _image_size(data: bytes) -> tuple[int, int] | None:
    try:
        probe = subprocess.run(
            [
                os.getenv("KTV_FFPROBE", "ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                "-i",
                "pipe:0",
            ],
            input=data,
            capture_output=True,
            check=True,
            timeout=5,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        w, h = int(stream["width"]), int(stream["height"])
        if (
            0 < w <= MAX_SOURCE_SIDE
            and 0 < h <= MAX_SOURCE_SIDE
            and w * h <= MAX_SOURCE_PIXELS
        ):
            return w, h
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
    ):
        pass
    return None


def normalize_image(data: bytes) -> bytes | None:
    """Encode at <=720x720 and <=256 KiB, or reject without storing."""
    if (
        not data
        or len(data) > MAX_DOWNLOAD
        or not (
            data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        )
    ):
        return None
    if _image_size(data) is None:
        return None
    for quality in (5, 10, 17):
        try:
            encoded = subprocess.run(
                [
                    os.getenv("KTV_FFMPEG", "ffmpeg"),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-threads",
                    "1",
                    "-i",
                    "pipe:0",
                    "-vf",
                    f"scale=w='min({MAX_SIDE},iw)':h='min({MAX_SIDE},ih)':force_original_aspect_ratio=decrease",
                    "-frames:v",
                    "1",
                    "-pix_fmt",
                    "yuvj420p",
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    str(quality),
                    "-f",
                    "mjpeg",
                    "pipe:1",
                ],
                input=data,
                capture_output=True,
                check=True,
                timeout=12,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        if encoded.startswith(b"\xff\xd8\xff") and 0 < len(encoded) <= MAX_JPEG:
            return encoded
    return None


def save_cover(directory: Path, song_id: int, data: bytes) -> bool:
    """Atomic JPEG-only save; never recreate a deleted event's media directory."""
    if (
        directory.is_symlink()
        or not directory.parent.is_dir()
        or directory.parent.is_symlink()
    ):
        return False
    try:
        directory.mkdir(exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".cover-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            os.replace(temp, directory / f"{song_id}.jpg")
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return True
    except OSError:
        return False


async def download_cover(
    client: httpx.AsyncClient, url: str, directory: Path, song_id: int
) -> bool:
    host = _cover_host(url)
    if not host or directory.is_symlink():
        return False
    headers = {
        "Referer": "https://www.bilibili.com/"
        if host.endswith(".hdslb.com")
        else "https://music.163.com/"
    }
    try:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";")[0].lower() not in {
                "image/jpeg",
                "image/jpg",
                "image/png",
                "image/webp",
            }:
                return False
            size = response.headers.get("content-length", "")
            if size.isdigit() and int(size) > MAX_DOWNLOAD:
                return False
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_DOWNLOAD:
                    return False
        encoded = await asyncio.to_thread(normalize_image, bytes(data))
        return bool(encoded) and await asyncio.to_thread(
            save_cover, directory, song_id, encoded
        )
    except (httpx.HTTPError, OSError, ValueError):
        return False


def normalize_existing_cover(path: Path) -> bool:
    """Convert historical cover to bounded JPEG in place."""
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
        return False
    try:
        if path.stat().st_size > MAX_DOWNLOAD:
            return False
        encoded = normalize_image(path.read_bytes())
        if not encoded or not save_cover(path.parent, int(path.stem), encoded):
            return False
        if path.suffix.lower() != ".jpg":
            path.unlink(missing_ok=True)
        return True
    except (OSError, ValueError):
        return False


def reuse_event_cover(event_folder: Path, song_id: int, url: str) -> bool:
    """Hardlink another request's identical cover within the same event."""
    directory = event_folder / "media" / "covers"
    if directory.is_symlink() or not (event_folder / "room.sqlite3").is_file():
        return False
    try:
        with sqlite3.connect(
            f"file:{event_folder / 'room.sqlite3'}?mode=ro", uri=True
        ) as db:
            ids = db.execute(
                "SELECT id FROM songs WHERE cover=? AND id<>? LIMIT 100",
                (url, song_id),
            ).fetchall()
        for (other_id,) in ids:
            other = cover_file(directory, other_id)
            if other and other.suffix.lower() == ".jpg":
                directory.mkdir(exist_ok=True)
                with contextlib.suppress(FileExistsError):
                    os.link(other, directory / f"{song_id}.jpg")
                return True
    except (OSError, sqlite3.Error):
        pass
    return False


class CoverCache:
    """Bounded two-thread cover work, independent of web requests and media workers."""

    def __init__(self, events):
        self.events = events
        self._pool: ThreadPoolExecutor | None = None
        self._users = 0
        self._lock = threading.Lock()
        self._capacity = threading.BoundedSemaphore(256)
        self.transport: httpx.AsyncBaseTransport | None = None

    def start(self) -> None:
        with self._lock:
            self._users += 1
            if self._pool:
                return
            self._pool = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="ktv-cover"
            )
        # Resume covers for an active event on service restart.
        if self.events.active_id:
            event_id = self.events.active_id
            store = self.events.active()
            with store.lock:
                rows = store.rows("SELECT id,cover FROM songs WHERE cover!=''")
            for song in rows:
                self.enqueue(event_id, song["id"], song["cover"])

    def enqueue(self, event_id: str, song_id: int, url: str) -> None:
        if (
            not _cover_host(url)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", event_id)
            or song_id < 1
        ):
            return
        with self._lock:
            pool = self._pool
            if pool is None:
                return
            if not self._capacity.acquire(blocking=False):
                _LOG.warning("Cover queue full; remote URL remains available")
                return
            try:
                future = pool.submit(self._cache, event_id, song_id, url)
            except RuntimeError:
                self._capacity.release()
                return
            future.add_done_callback(lambda _: self._capacity.release())

    def _cache(self, event_id: str, song_id: int, url: str) -> None:
        event_folder = (
            self.events.root if event_id == "legacy" else self.events.base / event_id
        )
        if not event_folder.is_dir() or event_folder.is_symlink():
            return
        covers = event_folder / "media" / "covers"
        if cover_file(covers, song_id):
            return
        if reuse_event_cover(event_folder, song_id, url):
            return

        async def fetch() -> bool:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=3),
                follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0 VC-KTV/0.5"},
                transport=self.transport,
            ) as client:
                return await download_cover(client, url, covers, song_id)

        try:
            if not asyncio.run(fetch()):
                _LOG.debug("Cover cache unavailable for %s/%d", event_id, song_id)
        except (OSError, sqlite3.Error, RuntimeError):
            _LOG.debug("Cover cache failed for %s/%d", event_id, song_id)

    def stop(self) -> None:
        with self._lock:
            self._users -= 1
            if self._users > 0:
                return
            pool, self._pool = self._pool, None
            self._users = 0
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
