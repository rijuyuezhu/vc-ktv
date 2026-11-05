"""Global-admin-only NetEase QR login, with a private persistent cookie file.

The cookie never goes to the browser, database, logs or source tree.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode

import httpx
import qrcode
import qrcode.image.svg

from .netease_weapi import encode_weapi_form

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
        self.transport = transport
        self.qr_requires_verification = False
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
            self.qr_requires_verification = False
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
                **(
                    {"phase": "verification_required"}
                    if self.qr_requires_verification
                    else {}
                ),
            }

    def start_qr(self) -> dict:
        with self.lock:
            self._expire()
            if self.music_cookie:
                return self.status()
            if not self.qr_key:
                self.qr_requires_verification = False
                response = self.client.post(
                    f"{QR_ORIGIN}/weapi/login/qrcode/unikey",
                    data=encode_weapi_form({"type": 1, "noCheckToken": True}),
                    headers={"Origin": QR_ORIGIN},
                )
                response.raise_for_status()
                data = response.json()
                key = data.get("unikey") if isinstance(data, dict) else None
                if not isinstance(key, str) or not QR_KEY_PATTERN.fullmatch(key):
                    raise NeteaseLoginError("网易云未返回有效的登录二维码")
                chain_id = f"v1_{secrets.token_hex(26).upper()}_web_login_{int(time.time() * 1000)}"
                scan_url = f"{QR_ORIGIN}/login?{urlencode({'codekey': key, 'chainId': chain_id})}"
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
            if self.qr_requires_verification:
                return {
                    **self.status(),
                    "phase": "verification_required",
                    "provider_code": 8821,
                }
            response = self.client.post(
                f"{QR_ORIGIN}/weapi/login/qrcode/client/login",
                data=encode_weapi_form(
                    {"key": self.qr_key, "type": 1, "noCheckToken": True}
                ),
                headers={"Origin": QR_ORIGIN},
            )
            response.raise_for_status()
            data = response.json()
            code = data.get("code") if isinstance(data, dict) else None
            if code == 8821:
                self.qr_requires_verification = True
                return {
                    **self.status(),
                    "phase": "verification_required",
                    "provider_code": 8821,
                }
            if code == 803:
                cookie = _read_music_cookie(response, self.client)
                if not cookie or not 10 <= len(cookie) <= 4096:
                    raise NeteaseLoginError(
                        "网易云已确认扫码，但没有返回 MUSIC_U 登录凭据，请重新扫码"
                    )
                self._save_cookie(cookie)
                self.music_cookie = cookie
                self.qr_requires_verification = False
                self.qr_key = None
                self.qr_image = None
                self.qr_expires = 0
                return {**self.status(), "phase": "connected"}
            if code == 800:
                self.qr_requires_verification = False
                self.qr_key = None
                self.qr_image = None
                self.qr_expires = 0
                return {**self.status(), "phase": "expired"}
            if code not in (801, 802):
                if isinstance(code, int) and not isinstance(code, bool):
                    raise NeteaseLoginError(
                        f"网易云拒绝扫码登录（状态码 {code}），可取消后重新扫码"
                    )
                raise NeteaseLoginError("网易云返回了未知的扫码状态，请稍后再试")
            return {**self.status(), "phase": "confirm" if code == 802 else "waiting"}

    def import_cookie(self, raw_value: str) -> dict:
        """Validate an account's browser MUSIC_U directly with NetEase before storing.

        Only a global-admin API may invoke this. No credentials are returned.
        """
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        if value.startswith("MUSIC_U="):
            value = value[len("MUSIC_U=") :]
        if not 10 <= len(value) <= 4096 or any(
            not 33 <= ord(char) <= 126 or char in ';,\\"' for char in value
        ):
            raise NeteaseLoginError("MUSIC_U 格式不正确，请只复制 Cookie 的 Value")
        with self.lock:
            with httpx.Client(
                timeout=httpx.Timeout(8, connect=3),
                follow_redirects=False,
                transport=self.transport,
                headers={"User-Agent": "Mozilla/5.0", "Referer": QR_ORIGIN + "/"},
                cookies={"MUSIC_U": value},
            ) as client:
                response = client.get(f"{QR_ORIGIN}/api/nuser/account/get")
                response.raise_for_status()
                result = response.json()
            if (
                not isinstance(result, dict)
                or result.get("code") != 200
                or not isinstance(result.get("account"), dict)
                or not isinstance(result["account"].get("id"), int)
            ):
                raise NeteaseLoginError("网易云未认可该登录态，请先在官方网页确认登录")
            self._save_cookie(value)
            self.music_cookie = value
            self.qr_requires_verification = False
            self.qr_key = None
            self.qr_image = None
            self.qr_expires = 0
            self.client.cookies.clear()
            return self.status()

    def disconnect(self) -> dict:
        with self.lock:
            self.qr_requires_verification = False
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
