"""Guest and administrator cancellation endpoints."""

from fastapi import APIRouter, Cookie


def create_cancellation_router(
    *, playback, require_admin, require_user, stop_song_processing
):
    router = APIRouter()

    @router.post("/api/songs/{song_id}/cancel")
    def cancel_own_song(song_id: int, vc_guest: str | None = Cookie(default=None)):
        user = require_user(vc_guest)
        result = playback.cancel_song(song_id, guest_id=user["id"])
        stop_song_processing(song_id)
        return result

    @router.post("/api/admin/songs/{song_id}/cancel")
    def cancel(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        result = playback.cancel_song(song_id)
        stop_song_processing(song_id)
        return result

    return router
