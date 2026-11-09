"""Administrator login, logout and authenticated state."""

from fastapi import APIRouter, Cookie, Request, Response

from vc_ktv.api.state_response import state_response
from vc_ktv.application.auth import AuthService
from vc_ktv.application.schemas import Login


def create_admin_session_router(*, auth: AuthService, events, media):
    router = APIRouter()

    @router.post("/api/admin/login")
    def login(body: Login, request: Request, response: Response):
        identity = auth.authenticate_admin(body.username, body.password, request)
        auth.admin_cookie(response, identity)
        return {"ok": True}

    @router.post("/api/admin/logout")
    def logout(response: Response):
        response.delete_cookie("vc_admin", path="/")
        return {"ok": True}

    @router.get("/api/admin/state")
    def admin_state(request: Request, vc_admin: str | None = Cookie(default=None)):
        auth.require_admin(vc_admin)
        if not events.active_id:
            return {
                "current": None,
                "queue": [],
                "waiting": [],
                "guests": [],
                "active": None,
            }
        return state_response(request, media.state_with_active())

    return router
