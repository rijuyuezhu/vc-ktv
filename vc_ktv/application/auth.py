"""Per-application authentication and signed session capabilities."""

import hashlib
import hmac
import ipaddress
import os
import sqlite3
from urllib.parse import quote

from fastapi import Request, Response

from vc_ktv.application.common import bail
from vc_ktv.core.accounts import verify_password
from vc_ktv.core.store import time_now


class AuthService:
    def __init__(self, events, store, password: str, secret: bytes):
        self.events = events
        self.store = store
        self.password = password
        self.secret = secret
        self.failed_login = {}
        self.failed_invites = {}
        self.failed_users = {}
        self.trusted_proxy_ips = {
            str(ipaddress.ip_address(value.strip()))
            for value in os.getenv("VC_KTV_TRUSTED_PROXY_IPS", "").split(",")
            if value.strip()
        }

    def client_ip(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if peer in self.trusted_proxy_ips:
            # Only an explicitly trusted proxy may supply the real peer address.
            forwarded = request.headers.get("x-real-ip", "")
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return peer

    def sign(self, kind: str, value: str, ttl: int) -> str:
        expiry = str(time_now() + ttl)
        payload = f"{kind}.{value}.{expiry}"
        sig = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + sig

    def read_signed(self, token: str | None, kind: str) -> str | None:
        if not token:
            return None
        try:
            k, value, exp, mac = token.split(".")
            msg = f"{k}.{value}.{exp}"
            correct = hmac.new(self.secret, msg.encode(), hashlib.sha256).hexdigest()
            if (
                k == kind
                and int(exp) >= time_now()
                and hmac.compare_digest(mac, correct)
            ):
                return value
        except (ValueError, TypeError):
            pass
        return None

    def require_guest(self, token: str | None) -> str | None:
        raw = self.read_signed(token, "guest")
        prefix = self.events.active_id + ":" if self.events.active_id else ""
        return raw[len(prefix) :] if raw and prefix and raw.startswith(prefix) else None

    def current_user(self, token: str | None) -> dict | None:
        guest = self.require_guest(token)
        if not guest or not self.events.active_id:
            return None
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT g.id,g.name FROM users u JOIN guests g ON g.id=u.id WHERE u.id=?",
                (guest,),
            ).fetchone()
        return dict(row) if row else None

    def require_user(self, token: str | None) -> dict:
        user = self.current_user(token)
        if not user:
            bail(401, "请先加入本场 KTV 并登录")
        return user

    def require_entry(self, token: str | None) -> None:
        if (
            not self.events.active_id
            or self.read_signed(token, "entry") != self.events.active_id
        ):
            bail(401, "请先输入本场六位加入码")

    def user_cookie(self, response: Response, user_id: str, event_id: str) -> None:
        ttl = 7 * 86400
        response.set_cookie(
            "vc_guest",
            self.sign("guest", f"{event_id}:{user_id}", ttl),
            max_age=ttl,
            httponly=True,
            samesite="strict",
            path="/",
        )

    def admin_principal(self, token: str | None) -> dict | None:
        """Check both the event scope and live account revision."""
        identity = self.read_signed(token, "admin")
        if identity == "yes":
            return {"role": "global"}
        if not identity or not self.events.active_id:
            return None
        parts = identity.split(":")
        if len(parts) != 3 or parts[0] != self.events.active_id:
            return None
        event_id, account_id, revision = parts
        with self.store.lock:
            row = self.store.db.execute(
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

    def require_admin(self, token: str | None) -> dict:
        principal = self.admin_principal(token)
        if principal is None:
            bail(401, "需要管理员登录")
        return principal

    def require_global_admin(self, token: str | None) -> dict:
        principal = self.require_admin(token)
        if principal["role"] != "global":
            bail(403, "仅全局管理员可操作")
        return principal

    def authenticate_admin(
        self, username: str | None, password: str, request: Request
    ) -> str:
        ip = self.client_ip(request)
        key = (ip, username.strip() if username else "")
        recent = [t for t in self.failed_login.get(key, []) if t > time_now() - 300]
        if len(recent) >= 10:
            bail(429, "登录尝试过于频繁，请稍后重试")
        identity = None
        if username is None or not username.strip():
            if hmac.compare_digest(password, self.password):
                identity = "yes"
        elif self.events.active_id:
            with self.store.lock:
                account = self.store.db.execute(
                    "SELECT id,password_hash,revision FROM event_admins WHERE username=?",
                    (username.strip(),),
                ).fetchone()
                if (
                    account
                    and len(password) <= 72
                    and verify_password(password, account["password_hash"])
                ):
                    identity = (
                        f"{self.events.active_id}:{account['id']}:{account['revision']}"
                    )
        if identity is None:
            recent.append(time_now())
            self.failed_login[key] = recent
            bail(401, "用户名或密码错误")
        self.failed_login.pop(key, None)
        return identity

    def admin_cookie(self, response: Response, identity: str) -> None:
        ttl = 7 * 86400
        response.set_cookie(
            "vc_admin",
            self.sign("admin", identity, ttl),
            max_age=ttl,
            httponly=True,
            samesite="strict",
            path="/",
        )

    def archived_guest_event(self, token: str | None) -> dict | None:
        identity = self.read_signed(token, "guest")
        if not identity or ":" not in identity:
            return None
        event_id, user_id = identity.split(":", 1)
        if event_id == self.events.active_id:
            return None
        with self.events.lock:
            event = next(
                (
                    e
                    for e in self.events.list()
                    if e["id"] == event_id and e["status"] == "closed"
                ),
                None,
            )
            if not event:
                return None
            try:
                path = self.events.database_path(event_id)
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
