"""Song-source search, previews and submissions (HTTP boundaries only)."""

import re
from collections.abc import Callable

import httpx
from fastapi import APIRouter, Cookie
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from vc_ktv.application.errors import bail
from vc_ktv.application.orders import OrderService
from vc_ktv.core.titles import title_key
from vc_ktv.sources import get_source

BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")


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
    page: int = Field(default=1, ge=1)


class RequestSong(Preview):
    nickname: str = Field(default="", max_length=24)


class RequestNetEaseSong(BaseModel):
    ref: str = Field(min_length=1, max_length=2048)


def create_song_source_router(
    app,
    *,
    current_user: Callable,
    admin_principal: Callable,
    require_user: Callable,
    orders: OrderService,
) -> APIRouter:
    router = APIRouter()
    events = app.state.events

    def require_source_access(vc_guest, vc_admin):
        if current_user(vc_guest) is None and admin_principal(vc_admin) is None:
            require_user(vc_guest)

    @router.get("/api/preview")
    async def preview(
        bvid: str, page: int = 1, vc_guest: str | None = Cookie(default=None)
    ):
        await run_in_threadpool(require_user, vc_guest)
        return await app.state.lookup(get_bvid(bvid), page)

    @router.get("/api/netease/search")
    async def netease_search(
        q: str,
        page: int = 0,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        await run_in_threadpool(require_source_access, vc_guest, vc_admin)
        if not q.strip() or page < 0:
            bail(400, "请输入歌名或歌手")
        try:
            results = (
                await app.state.netease_search(q)
                if page == 0
                else await app.state.netease_search(q, page=page)
            )
            return {"songs": results}
        except (httpx.HTTPError, ValueError, TypeError):
            bail(502, "网易云搜索暂不可用")

    @router.get("/api/netease/preview")
    async def netease_preview(
        ref: str,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        # Read-only previews are available to joined users and either admin role.
        # Account connection and credential endpoints will be global-admin-only.
        await run_in_threadpool(require_source_access, vc_guest, vc_admin)
        try:
            song_id = await app.state.netease_resolve(ref)
        except ValueError as error:
            bail(400, str(error))
        except httpx.HTTPError:
            bail(502, "网易云分享链接暂时无法解析")
        try:
            return await app.state.netease_lookup(song_id)
        except LookupError:
            bail(404, "网易云未找到该单曲")
        except (httpx.HTTPError, ValueError, TypeError):
            bail(502, "网易云歌曲信息暂不可用")

    @router.post("/api/songs")
    async def request_song(
        body: RequestSong,
        vc_guest: str | None = Cookie(default=None),
    ):
        user = await run_in_threadpool(require_user, vc_guest)
        guest = user["id"]
        event_id = events.active_id
        await run_in_threadpool(orders.precheck_new_order, guest)
        info = await app.state.lookup(get_bvid(body.bvid), body.page)
        metadata = get_source("bilibili").metadata(info, ref=body.bvid, page=body.page)
        new_id = await run_in_threadpool(
            orders.submit,
            guest,
            event_id,
            metadata,
            changed_message="场次已变化，请在新场次重新点歌",
        )
        app.state.covers.enqueue(event_id, new_id, metadata.cover)
        return {
            "id": new_id,
            "status": "waiting",
            "title": info["title"],
            "lyric_query": title_key(info["title"]),
        }

    @router.post("/api/netease/songs")
    async def request_netease_song(
        body: RequestNetEaseSong,
        vc_guest: str | None = Cookie(default=None),
    ):
        user = await run_in_threadpool(require_user, vc_guest)
        try:
            song_id = await app.state.netease_resolve(body.ref)
        except ValueError as error:
            bail(400, str(error))
        except httpx.HTTPError:
            bail(502, "网易云分享链接暂时无法解析")
        event_id = events.active_id
        await run_in_threadpool(orders.precheck_new_order, user["id"])
        try:
            info = await app.state.netease_lookup(song_id)
        except LookupError:
            bail(404, "网易云未找到该单曲")
        except (httpx.HTTPError, ValueError, TypeError):
            bail(502, "网易云歌曲信息暂不可用")
        metadata = get_source("netease").metadata(info, ref=str(song_id))
        new_id = await run_in_threadpool(
            orders.submit,
            user["id"],
            event_id,
            metadata,
            changed_message="场次已变化，请重新点歌",
        )
        app.state.covers.enqueue(event_id, new_id, metadata.cover)
        return {
            "id": new_id,
            "status": "waiting",
            "title": info["title"],
            "lyric_query": title_key(info["title"]),
            "source": "netease",
        }

    return router
