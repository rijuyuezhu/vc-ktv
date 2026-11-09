"""Global-admin NetEase login endpoints; credentials stay server-side."""

from collections.abc import Callable

import httpx
from fastapi import APIRouter, Cookie, Response
from pydantic import BaseModel, Field

from vc_ktv.application.errors import bail
from vc_ktv.sources.netease_login import NeteaseLogin, NeteaseLoginError


class ImportNetEaseCookie(BaseModel):
    music_u: str = Field(min_length=1, max_length=4104)


def create_netease_admin_router(
    netease_login: NeteaseLogin,
    require_global_admin: Callable[[str | None], object],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/netease/status")
    def netease_session_status(
        response: Response, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        response.headers["Cache-Control"] = "no-store"
        return netease_login.status()

    @router.post("/api/admin/netease/qr")
    def netease_session_qr(
        response: Response, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        response.headers["Cache-Control"] = "no-store"
        try:
            return netease_login.start_qr()
        except (httpx.HTTPError, NeteaseLoginError, ValueError):
            bail(502, "网易云登录二维码暂不可用")

    @router.post("/api/admin/netease/check")
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

    @router.post("/api/admin/netease/import")
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

    @router.post("/api/admin/netease/disconnect")
    def netease_session_disconnect(
        response: Response, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        response.headers["Cache-Control"] = "no-store"
        return netease_login.disconnect()

    return router
