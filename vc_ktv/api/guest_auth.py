"""Event entry and guest sessions; preserve per-event signed cookies."""

import hmac
import secrets
import sqlite3

from fastapi import APIRouter, Cookie, Request, Response
from pydantic import BaseModel, Field

from vc_ktv.application.auth import AuthService
from vc_ktv.application.errors import bail
from vc_ktv.core.accounts import password_hash, verify_password
from vc_ktv.core.store import time_now


class JoinEvent(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class RegisterUser(BaseModel):
    username: str = Field(min_length=1, max_length=24)
    password: str = Field(min_length=1, max_length=72)
    confirm_password: str = Field(min_length=1, max_length=72)


class UserLogin(BaseModel):
    username: str = Field(min_length=1, max_length=24)
    password: str = Field(min_length=1, max_length=72)


def create_guest_auth_router(*, auth: AuthService, events, store) -> APIRouter:
    router = APIRouter()

    @router.get("/api/entry")
    def entry(
        vc_join: str | None = Cookie(default=None),
        vc_guest: str | None = Cookie(default=None),
    ):
        event = events.metadata()
        archive = auth.archived_guest_event(vc_guest)
        if not event:
            return {
                "event": None,
                "joined": False,
                "authenticated": False,
                "username": None,
                "archived_event": archive,
            }
        user = auth.current_user(vc_guest)
        joined = bool(user or auth.read_signed(vc_join, "entry") == events.active_id)
        return {
            "event": {"id": event["id"], "name": event["name"]},
            "joined": joined,
            "authenticated": bool(user),
            "username": user["name"] if user else None,
            "archived_event": archive if not user else None,
        }

    @router.post("/api/join")
    def join_event(body: JoinEvent, request: Request, response: Response):
        info = events.metadata()
        if not info or info.get("legacy") or not info.get("code"):
            bail(409, "请等待管理员创建新 KTV 场次")
        ip = auth.client_ip(request)
        recent = [t for t in auth.failed_invites.get(ip, []) if t > time_now() - 300]
        if len(recent) >= 10:
            bail(429, "尝试过于频繁，请稍后重试")
        if (
            not body.code.isascii()
            or not body.code.isdecimal()
            or not hmac.compare_digest(body.code, info["code"])
        ):
            recent.append(time_now())
            auth.failed_invites[ip] = recent
            bail(401, "加入码错误")
        auth.failed_invites.pop(ip, None)
        event_id = info["id"]
        if events.active_id != event_id:
            bail(409, "场次已变化，请重新输入加入码")
        response.set_cookie(
            "vc_join",
            auth.sign("entry", event_id, 3600),
            max_age=3600,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"ok": True, "event": {"id": info["id"], "name": info["name"]}}

    @router.post("/api/users/register")
    def register_user(
        body: RegisterUser,
        response: Response,
        vc_join: str | None = Cookie(default=None),
    ):
        auth.require_entry(vc_join)
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
            auth.require_entry(vc_join)
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
        auth.user_cookie(response, user_id, event_id)
        return {"ok": True, "username": name}

    @router.post("/api/users/login")
    def login_user(
        body: UserLogin,
        request: Request,
        response: Response,
        vc_join: str | None = Cookie(default=None),
    ):
        auth.require_entry(vc_join)
        ip = auth.client_ip(request)
        key = (events.active_id, ip, body.username.strip())
        recent = [t for t in auth.failed_users.get(key, []) if t > time_now() - 300]
        if len(recent) >= 10:
            bail(429, "尝试过于频繁，请稍后重试")
        with store.lock:
            auth.require_entry(vc_join)
            event_id = events.active_id
            row = store.db.execute(
                "SELECT id,username,password_hash FROM users WHERE username=?",
                (body.username.strip(),),
            ).fetchone()
        if not row or not verify_password(body.password, row["password_hash"]):
            recent.append(time_now())
            auth.failed_users[key] = recent
            bail(401, "用户名或密码错误")
        auth.failed_users.pop(key, None)
        auth.user_cookie(response, row["id"], event_id)
        return {"ok": True, "username": row["username"]}

    @router.post("/api/users/logout")
    def logout_user(response: Response):
        response.delete_cookie("vc_guest", path="/")
        response.delete_cookie("vc_join", path="/")
        return {"ok": True}

    return router
