"""Playback HTTP endpoints; PlaybackService owns all state changes."""

from fastapi import APIRouter, Cookie

from vc_ktv.application.common import bail
from vc_ktv.application.playback import PlaybackService
from vc_ktv.application.schemas import Finish, PlaybackMode


def create_playback_router(
    *, playback: PlaybackService, require_admin, require_user, active_view
):
    router = APIRouter()
    store = playback.store

    @router.put("/api/admin/playback-mode")
    def change_playback_mode(
        body: PlaybackMode, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        return playback.change_mode(body.mode)

    @router.get("/api/admin/played")
    def recently_played(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return playback.recently_played()

    @router.post("/api/admin/songs/{song_id}/pin-next")
    def pin_next_song(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return playback.pin_next(song_id)

    @router.post("/api/songs/{song_id}/replay")
    def replay_own_turn(song_id: int, vc_guest: str | None = Cookie(default=None)):
        user = require_user(vc_guest)
        return playback.schedule_replay(song_id, guest_id=user["id"])

    @router.post("/api/admin/songs/{song_id}/replay")
    def replay_song(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return playback.schedule_replay(song_id)

    @router.post("/api/admin/songs/{song_id}/replay-next")
    def replay_current(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "电视模式请使用主屏遥控器")
            return playback.schedule_replay(song_id, current_only=True)

    @router.post("/api/admin/next")
    def next_song(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "当前由电视主屏播放")
            playback.claim_next()
            return {"active": active_view()}

    @router.post("/api/admin/finish")
    def finish_song(body: Finish, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "当前由电视主屏播放")
            return playback.finish(body.song_id, skipped=body.reason == "skip")

    return router
