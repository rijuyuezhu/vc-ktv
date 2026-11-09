"""Event lifecycle and archive administration; preserve transition semantics."""

from typing import Literal

from fastapi import APIRouter, Cookie
from pydantic import BaseModel, Field

from vc_ktv.application.event_lifecycle import EventLifecycle
from vc_ktv.rendering.subtitle_styles import StylePreset


class EventCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    code: str | None = None


class PlaybackSettings(BaseModel):
    queue_policy: Literal["fair", "fcfs"] | None = None
    allow_unsubtitled: bool | None = None
    # Old clients that omit the new field must not reset the chosen preset.
    subtitle_style: StylePreset | None = None
    video_quality: Literal[720, 480] | None = None
    # Optional to preserve older admin clients' current per-event setting.
    max_bandwidth_mbps: float | None = Field(default=None, ge=1)
    orders_paused: bool | None = None
    tv_html_overlay: bool | None = None
    force_h264_originals: bool | None = None


def create_event_admin_router(
    *,
    store,
    events,
    require_admin,
    require_global_admin,
    archive,
    lifecycle: EventLifecycle,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/settings")
    def get_playback_settings(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            return store.settings()

    @router.put("/api/admin/settings")
    def update_playback_settings(
        body: PlaybackSettings, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        with store.lock:
            return store.save_settings(
                body.queue_policy,
                body.allow_unsubtitled,
                body.subtitle_style,
                body.video_quality,
                body.max_bandwidth_mbps,
                body.orders_paused,
                body.tv_html_overlay,
                body.force_h264_originals,
            )

    @router.post("/api/admin/events/{event_id}/release-media")
    def release_event_media(event_id: str, vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        with events.lock:
            events.closed_folder(event_id)
        csv_data = archive.csv_text(event_id, vc_admin)
        return events.release_media(event_id, csv_data)

    @router.delete("/api/admin/events/{event_id}")
    def delete_event_record(event_id: str, vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        return events.delete_closed(event_id)

    @router.post("/api/admin/events")
    def create_event(body: EventCreate, vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        return lifecycle.create(body.name, body.code)

    @router.post("/api/admin/events/close")
    def close_event(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return lifecycle.close()

    return router
