"""Global-admin-only NetEase QR login, with a private persistent cookie file.

The cookie never goes to the browser, database, logs or source tree.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path

import httpx
import qrcode
import qrcode.image.svg

QR_TTL_SECONDS = 180
QR_KEY_PATTERN = re.compile(r"[a-zA-Z0-9_=-]{8,256}\Z")
QR_ORIGIN = "https://music.163.com"


class NeteaseLoginError(ValueError):
    """The upstream login protocol returned an invalid or unsupported state."""


def _read_music_cookie(response: httpx.Response, client: httpx.Client) -> str | None:
    try:
        raw = response.json().get("cookie")
        if isinstance(raw, str):
            parsed = SimpleCookie()
            parsed.load(raw)
            value = parsed.get("MUSIC_U")
            if value:
                return value.value
        return client.cookies.get("MUSIC_U")
    except (ValueError, TypeError):
        return None


class NeteaseLogin:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        clock=time.monotonic,
        storage_path: Path | None = None,
    ):
        self.lock = threading.RLock()
        self.clock = clock
        self.storage_path = Path(storage_path) if storage_path is not None else None
        self.client = httpx.Client(
            timeout=httpx.Timeout(8, connect=3),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
        )
        self.qr_key: str | None = None
        self.qr_image: str | None = None
        self.qr_expires = 0.0
        self.music_cookie: str | None = self._load_cookie()

    def _load_cookie(self) -> str | None:
        path = self.storage_path
        if path is None or not path.exists():
            return None
        # Treat a backup/share exposed as group/world readable as invalid.
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise NeteaseLoginError("网易云登录凭据文件权限不安全")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))["MUSIC_U"]
            return (
                value if isinstance(value, str) and 10 <= len(value) <= 4096 else None
            )
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _save_cookie(self, cookie: str) -> None:
        path = self.storage_path
        if path is None:
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Atomic replacement and 0600 permissions also survive process crashes.
        fd, tmp = tempfile.mkstemp(prefix=".netease-session-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(json.dumps({"MUSIC_U": cookie}))
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _expire(self) -> None:
        now = self.clock()
        if self.qr_key and now >= self.qr_expires:
            self.qr_key = None
            self.qr_image = None
            self.qr_expires = 0

    def cookies(self) -> dict[str, str] | None:
        """Worker access: the saved MUSIC_U, never the QR state."""
        with self.lock:
            self._expire()
            return {"MUSIC_U": self.music_cookie} if self.music_cookie else None

    def status(self) -> dict:
        with self.lock:
            self._expire()
            now = self.clock()
            return {
                "connected": bool(self.music_cookie),
                "pending": bool(self.qr_key),
                "qr_expires_in": max(0, int(self.qr_expires - now))
                if self.qr_key
                else 0,
            }

    def start_qr(self) -> dict:
        with self.lock:
            self._expire()
            if self.music_cookie:
                return self.status()
            if not self.qr_key:
                response = self.client.get(
                    f"{QR_ORIGIN}/api/login/qrcode/unikey", params={"type": 1}
                )
                response.raise_for_status()
                data = response.json()
                key = data.get("unikey") if isinstance(data, dict) else None
                if not isinstance(key, str) or not QR_KEY_PATTERN.fullmatch(key):
                    raise NeteaseLoginError("网易云未返回有效的登录二维码")
                scan_url = f"{QR_ORIGIN}/login?codekey={key}"
                qr = qrcode.QRCode(
                    box_size=8,
                    border=3,
                    error_correction=qrcode.constants.ERROR_CORRECT_M,
                )
                qr.add_data(scan_url)
                qr.make(fit=True)
                image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
                buffer = io.BytesIO()
                image.save(buffer)
                self.qr_image = "data:image/svg+xml;base64," + base64.b64encode(
                    buffer.getvalue()
                ).decode("ascii")
                self.qr_key = key
                self.qr_expires = self.clock() + QR_TTL_SECONDS
            return {**self.status(), "qr_image": self.qr_image}

    def poll(self) -> dict:
        with self.lock:
            self._expire()
            if self.music_cookie:
                return {**self.status(), "phase": "connected"}
            if not self.qr_key:
                return {**self.status(), "phase": "expired"}
            response = self.client.get(
                f"{QR_ORIGIN}/api/login/qrcode/client/login",
                params={"key": self.qr_key, "type": 1},
            )
            response.raise_for_status()
            data = response.json()
            code = data.get("code") if isinstance(data, dict) else None
            if code == 803:
                cookie = _read_music_cookie(response, self.client)
                if not cookie or not 10 <= len(cookie) <= 4096:
                    raise NeteaseLoginError("网易云已确认扫码，但未返回有效登录凭据")
                self._save_cookie(cookie)
                self.music_cookie = cookie
                self.qr_key = None
                self.qr_image = None
                self.qr_expires = 0
                return {**self.status(), "phase": "connected"}
            if code == 800:
                self.qr_key = None
                self.qr_image = None
                self.qr_expires = 0
                return {**self.status(), "phase": "expired"}
            if code not in (801, 802):
                raise NeteaseLoginError("网易云扫码状态暂不可用")
            return {**self.status(), "phase": "confirm" if code == 802 else "waiting"}

    def disconnect(self) -> dict:
        with self.lock:
            self.qr_key = None
            self.qr_image = None
            self.qr_expires = 0
            self.music_cookie = None
            self.client.cookies.clear()
            if self.storage_path is not None:
                self.storage_path.unlink(missing_ok=True)
            return self.status()

    def close(self) -> None:
        """Release network resources without discarding the saved login."""
        with self.lock:
            self.qr_key = None
            self.qr_image = None
            self.client.close()
