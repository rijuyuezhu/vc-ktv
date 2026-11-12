"""Read-only event-history HTTP endpoints."""

from fastapi import APIRouter, Cookie
from fastapi.responses import FileResponse

from vc_ktv.application.archive import EventArchiveService
from vc_ktv.application.errors import bail


def create_event_history_router(archive: EventArchiveService) -> APIRouter:
    router = APIRouter()
    events = archive.events

    @router.get("/api/events")
    def public_events(
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        return {"events": archive.public_events(vc_guest, vc_admin)}

    @router.get("/api/events/{event_id}/songs")
    def public_event_songs(
        event_id: str,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        return archive.public_records(event_id, vc_guest, vc_admin)

    @router.get("/api/events/{event_id}/songs/{song_id}")
    def public_event_song(
        event_id: str,
        song_id: int,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        return archive.song_detail(event_id, song_id, vc_guest, vc_admin)

    @router.get("/api/events/{event_id}/covers/{song_id}")
    def event_song_cover(
        event_id: str,
        song_id: int,
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        archive.require_cover_viewer(event_id, vc_guest, vc_admin)
        image = archive.cached_cover(event_id, song_id)
        if image is None:
            bail(404, "封面不存在")
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }[image.suffix.lower()]
        return FileResponse(image, media_type=media_type)

    @router.get("/api/admin/events")
    def list_events(vc_admin: str | None = Cookie(default=None)):
        principal = archive.auth.require_admin(vc_admin)
        return {
            "active": events.active_id,
            "events": events.list()
            if principal["role"] == "global"
            else [events.metadata()],
            "role": principal["role"],
            "username": principal.get("username"),
        }

    @router.get("/api/admin/events/{event_id}/songs")
    def event_song_history(event_id: str, vc_admin: str | None = Cookie(default=None)):
        return {"songs": archive.records(event_id, vc_admin)}

    @router.get("/api/admin/events/{event_id}/songs.csv")
    def export_event_songs(event_id: str, vc_admin: str | None = Cookie(default=None)):
        return archive.export(event_id, vc_admin)

    return router
