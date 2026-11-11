"""Thin TV HTTP adapters; session authority is owned by TVSessionService."""

import asyncio

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from vc_ktv.api.state_response import state_response
from vc_ktv.application.schemas import Finish, TVPair, TVRemoteCommand, TVRemoteReport
from vc_ktv.application.tv import TVSessionService


def create_tv_router(*, tv: TVSessionService, playback, media) -> APIRouter:
    router = APIRouter()
    store = tv.store

    async def events(request: Request, channel: str, authorize):
        # Streaming requests are not event-pinned: a long-lived connection must
        # never prevent a room from closing. Re-check authority on every wake.
        async def stream():
            with tv.notifications.listen(channel) as updates:
                yield "event: ready\ndata: {}\n\n"
                while not await request.is_disconnected():
                    try:
                        await asyncio.wait_for(updates.get(), timeout=10)
                        changed = True
                    except TimeoutError:
                        changed = False
                    try:
                        await run_in_threadpool(authorize)
                    except HTTPException:
                        yield "event: revoked\ndata: {}\n\n"
                        return
                    yield "event: update\ndata: {}\n\n" if changed else ": ping\n\n"

        await run_in_threadpool(authorize)
        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/api/tv/events")
    async def tv_events(request: Request, token: str):
        return await events(request, "tv", lambda: tv.require_tv(token))

    @router.get("/api/admin/tv/events")
    async def admin_events(
        request: Request, vc_admin: str | None = Cookie(default=None)
    ):
        event_id = tv.events.active_id

        def authorize():
            tv.auth.require_admin(vc_admin)
            if tv.events.active_id != event_id:
                raise HTTPException(401, "场次已切换")

        return await events(request, "admin", authorize)

    @router.get("/api/admin/tv")
    def tv_management(vc_admin: str | None = Cookie(default=None)):
        return tv.management(tv.auth.require_admin(vc_admin))

    @router.get("/api/admin/tv/remote")
    def tv_remote_state(vc_admin: str | None = Cookie(default=None)):
        return tv.remote_state(tv.auth.require_admin(vc_admin))

    @router.post("/api/admin/tv/remote")
    def tv_remote_send(
        body: TVRemoteCommand, vc_admin: str | None = Cookie(default=None)
    ):
        principal = tv.auth.require_admin(vc_admin)
        return tv.send_command(principal, body.action, body.song_id, body.value)

    @router.post("/api/tv/report")
    def tv_report(body: TVRemoteReport, token: str):
        tv.require_tv(token)
        return tv.report(body.ack_seq, body.model_dump())

    @router.get("/api/admin/tv-link")
    def tv_link(replace: bool = False, vc_admin: str | None = Cookie(default=None)):
        return tv.link(tv.auth.require_admin(vc_admin), replace)

    @router.post("/api/tv/activate")
    def tv_activate(body: TVPair):
        return tv.activate(body.pair)

    @router.post("/api/admin/tv/revoke")
    def tv_revoke(vc_admin: str | None = Cookie(default=None)):
        return tv.revoke(tv.auth.require_admin(vc_admin))

    @router.post("/api/admin/tv/restore-current")
    def tv_restore_current(vc_admin: str | None = Cookie(default=None)):
        return tv.restore_current(tv.auth.require_admin(vc_admin))

    @router.get("/api/tv/state")
    def tv_state(token: str, request: Request):
        tv.require_tv(token)
        return state_response(request, tv.state(media))

    @router.post("/api/tv/replay-next")
    def tv_replay_next(body: Finish, token: str):
        tv.require_tv(token)
        with store.lock:
            tv.require_tv_mode()
            return playback.schedule_replay(body.song_id, current_only=True)

    @router.get("/api/tv/preload")
    def tv_preload(token: str):
        tv.require_tv(token)
        with store.lock:
            if store.playback_mode() != "tv" or not store.current():
                return {"candidate": None}
            # Read-only prediction; do not advance fair credits or song states.
            queue = store.fair_queue()
            if not queue:
                return {"candidate": None}
            song = queue[0]
            media_row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song["id"],)
            ).fetchone()
            assets = store.replay_assets(media_row)
            return {
                "candidate": {
                    "id": song["id"],
                    "media_file": assets["media_file"],
                    "source_file": assets["source_file"],
                    "media_url": media.signed_media(song),
                    "html_overlay": media.overlay_view(dict(media_row)),
                }
            }

    @router.post("/api/tv/next")
    def tv_next(token: str):
        tv.require_tv(token)
        with store.lock:
            tv.require_tv_mode()
            playback.claim_next()
            return {"active": media.active_view()}

    @router.post("/api/tv/finish")
    def tv_finish(body: Finish, token: str):
        tv.require_tv(token)
        with store.lock:
            tv.require_tv_mode()
            return playback.finish(body.song_id, skipped=body.reason == "skip")

    return router
