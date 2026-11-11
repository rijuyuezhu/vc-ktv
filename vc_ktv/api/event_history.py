"""Read-only event-history HTTP endpoints."""

from fastapi import APIRouter, Cookie

from vc_ktv.application.archive import EventArchiveService


def create_event_history_router(archive: EventArchiveService) -> APIRouter:
    router = APIRouter()
    events = archive.events

    @router.get("/api/events")
    def public_events():
        return {"events": archive.public_events()}

    @router.get("/api/events/{event_id}/songs")
    def public_event_songs(event_id: str, vc_admin: str | None = Cookie(default=None)):
        return archive.public_records(event_id, vc_admin)

    @router.get("/api/events/{event_id}/songs/{song_id}")
    def public_event_song(
        event_id: str, song_id: int, vc_admin: str | None = Cookie(default=None)
    ):
        return archive.song_detail(event_id, song_id, vc_admin)

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
