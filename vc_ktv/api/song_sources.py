"""Song-source search, previews and submissions (HTTP boundaries only)."""

import sqlite3
from collections.abc import Callable

import httpx
from fastapi import APIRouter, Cookie, Response
from pydantic import BaseModel, Field

from vc_ktv.application.common import bail
from vc_ktv.core.store import time_now
from vc_ktv.core.titles import title_key
from vc_ktv.sources import get_source


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
    get_bvid: Callable[[str], str],
    current_user: Callable,
    admin_principal: Callable,
    require_user: Callable,
    check_new_order: Callable[[str], None],
) -> APIRouter:
    router = APIRouter()
    events = app.state.events
    store = app.state.store

    @router.get("/api/preview")
    async def preview(
        bvid: str, page: int = 1, vc_guest: str | None = Cookie(default=None)
    ):
        require_user(vc_guest)
        return await app.state.lookup(get_bvid(bvid), page)

    @router.get("/api/netease/search")
    async def netease_search(
        q: str,
        page: int = 0,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if current_user(vc_guest) is None and admin_principal(vc_admin) is None:
            require_user(vc_guest)
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
        if current_user(vc_guest) is None and admin_principal(vc_admin) is None:
            require_user(vc_guest)
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
        response: Response,
        vc_guest: str | None = Cookie(default=None),
    ):
        user = require_user(vc_guest)
        guest = user["id"]
        event_id = events.active_id
        with store.lock:
            check_new_order(guest)
        info = await app.state.lookup(get_bvid(body.bvid), body.page)
        metadata = get_source("bilibili").metadata(info, ref=body.bvid, page=body.page)
        with store.lock:
            if events.active_id != event_id:
                bail(409, "场次已变化，请在新场次重新点歌")
            # Recheck under the same lock as insertion: parallel requests
            # cannot overfill the room or bypass a newly enabled pause.
            check_new_order(guest)
            try:
                cur = store.db.execute(
                    """
                    INSERT INTO songs(
                        bvid,page,title,uploader,cover,duration,requester,requested_at,
                        source_type,source_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    metadata.db_values(guest, time_now()),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {
            "id": cur.lastrowid,
            "status": "waiting",
            "title": info["title"],
            "lyric_query": title_key(info["title"]),
        }

    @router.post("/api/netease/songs")
    async def request_netease_song(
        body: RequestNetEaseSong,
        vc_guest: str | None = Cookie(default=None),
    ):
        user = require_user(vc_guest)
        try:
            song_id = await app.state.netease_resolve(body.ref)
        except ValueError as error:
            bail(400, str(error))
        except httpx.HTTPError:
            bail(502, "网易云分享链接暂时无法解析")
        event_id = events.active_id
        with store.lock:
            check_new_order(user["id"])
        try:
            info = await app.state.netease_lookup(song_id)
        except LookupError:
            bail(404, "网易云未找到该单曲")
        except (httpx.HTTPError, ValueError, TypeError):
            bail(502, "网易云歌曲信息暂不可用")
        metadata = get_source("netease").metadata(info, ref=str(song_id))
        with store.lock:
            if events.active_id != event_id:
                bail(409, "场次已变化，请重新点歌")
            check_new_order(user["id"])
            try:
                cur = store.db.execute(
                    """INSERT INTO songs(
                         bvid,page,title,uploader,cover,duration,requester,requested_at,
                         source_type,source_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    metadata.db_values(user["id"], time_now()),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {
            "id": cur.lastrowid,
            "status": "waiting",
            "title": info["title"],
            "lyric_query": title_key(info["title"]),
            "source": "netease",
        }

    return router
