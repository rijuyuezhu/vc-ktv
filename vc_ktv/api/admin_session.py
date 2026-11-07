"""Administrator login, logout and authenticated state."""

import hmac

from fastapi import APIRouter, Cookie, HTTPException, Request, Response

from ..accounts import verify_password
from ..application.schemas import Login
from ..core.store import time_now


def create_admin_session_router(
    *,
    store,
    events,
    password,
    client_ip,
    sign,
    require_admin,
    state_with_active,
    failed_login,
):
    router = APIRouter()

    def bail(status: int, message: str):
        raise HTTPException(status_code=status, detail=message)

    @router.post("/api/admin/login")
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

    @router.post("/api/admin/logout")
    def logout(response: Response):
        response.delete_cookie("vc_admin", path="/")
        return {"ok": True}

    @router.get("/api/admin/state")
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

    return router
