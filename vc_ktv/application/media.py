"""Signed media views, subtitle archives and safe streamed file responses."""

from pathlib import Path
from urllib.parse import quote

from fastapi.responses import FileResponse

from vc_ktv.core.artifacts import bundle_archive, find_bundle, html_playback_source

MEDIA_MIMES = {".mp4": "video/mp4", ".webm": "video/webm", ".m4v": "video/mp4"}


class PinnedFileResponse(FileResponse):
    """Keep a media inode alive through FileResponse's deferred open/send."""

    async def __call__(self, scope, receive, send):
        # A pathsend event may be queued for later opening. Stream the file
        # here instead so the pinned link is safe to remove in finally.
        if "http.response.pathsend" in scope.get("extensions", {}):
            scope = {
                **scope,
                "extensions": {
                    key: value
                    for key, value in scope["extensions"].items()
                    if key != "http.response.pathsend"
                },
            }
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


class MediaService:
    def __init__(self, store, events, auth, media_pins):
        self.store = store
        self.events = events
        self.auth = auth
        self.media_pins = media_pins

    def pinned_response(self, path: Path, **kwargs) -> PinnedFileResponse:
        # The route pins while holding Store.lock; FileResponse opens later.
        pinned = self.media_pins.pin(path)
        return PinnedFileResponse(pinned, **kwargs)

    def signed_media(self, song: dict) -> str:
        token = self.auth.sign(
            "media", f"{self.events.active_id}:{song['id']}", 8 * 3600
        )
        return f"/media/{song['id']}?token={quote(token)}"

    def backing_asset(self, song: dict) -> tuple[str, str | None, Path | None]:
        return self.store.backing_asset(song)

    def backing_view(self, song: dict) -> dict:
        state, key, path = self.backing_asset(song)
        if state != "ready" or not path:
            return {"status": state}
        identity = f"{self.events.active_id}:{song['id']}:{key}"
        token = self.auth.sign("backing", identity, 8 * 3600)
        return {
            "status": "ready",
            "cache_key": key,
            "url": f"/media/{song['id']}/backing?token={quote(token)}",
        }

    def overlay_bundle(self, song: dict) -> Path | None:
        return find_bundle(self.store, song)

    @staticmethod
    def overlay_archive(bundle: Path) -> Path:
        return bundle_archive(bundle)

    def overlay_source(self, song: dict) -> Path | None:
        """Choose the source or its bitrate-capped cache for HTML subtitles."""
        return html_playback_source(self.store, song, self.store.settings())

    def overlay_view(self, song: dict) -> dict | None:
        source = self.overlay_source(song)
        if source is None:
            return None
        # Bind an issued URL to the selected rendition, not the mutable
        # bandwidth setting. Range requests must keep reading the same file.
        source_key = str(source.relative_to(self.store.media)).encode().hex()
        media_token = quote(
            self.auth.sign(
                "tv-source",
                f"{self.events.active_id}:{song['id']}:{source_key}",
                8 * 3600,
            )
        )
        tv_token = quote(
            self.auth.sign("overlay", f"{self.events.active_id}:{song['id']}", 8 * 3600)
        )
        return {
            "source_url": f"/media/{song['id']}/source?token={media_token}",
            "source_key": source_key,
            "timeline_url": f"/api/tv/subtitles/{song['id']}?token={tv_token}",
        }

    def active_view(self) -> dict | None:
        song = self.store.current()
        if not song:
            return None
        info = self.store.rows(
            """SELECT s.*, g.name AS nickname FROM songs s
                             JOIN guests g ON g.id=s.requester WHERE s.id=?""",
            (song["id"],),
        )[0]
        info = self.store.replay_assets(info)
        info["media_url"] = (
            None
            if info.get("html_only") and info["prep_state"] == "done"
            else self.signed_media(info)
        )
        info["html_overlay"] = self.overlay_view(info)
        info["backing"] = self.backing_view(info)
        return info

    def state_with_active(self) -> dict:
        # The middleware pins the event. Only the active Store mutex is needed
        # so a close request may begin its transition while readers finish.
        with self.events.active().lock:
            return {**self.store.state(), "active": self.active_view()}
