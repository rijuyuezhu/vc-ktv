"""Signed media views, subtitle archives and safe streamed file responses."""

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import FileResponse

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

    def overlay_bundle(self, song: dict) -> Path | None:
        """Find a compatible subtitle archive along the virtual replay lineage."""
        if song.get("prep_state") != "done" or song.get("skip_subtitles"):
            return None
        resolved = self.store.replay_assets(song)
        if not resolved.get("source_file") or not resolved.get("media_file"):
            return None
        source = self.store.media / Path(resolved["source_file"]).name
        video = self.store.media / Path(resolved["media_file"]).name
        for ancestor in self.store.replay_chain(song):
            # A replacement upload/render cannot reuse an older subtitle plan.
            # Legacy hardlinks remain compatible if physical files still match.
            for field, actual in (("source_file", source), ("media_file", video)):
                path = ancestor.get(field)
                if path:
                    try:
                        if not (self.store.media / Path(path).name).samefile(actual):
                            return None
                    except OSError:
                        return None
            if ancestor["id"] != song["id"] and any(
                ancestor.get(field) != song.get(field)
                for field in ("lyrics", "lyric_choice", "lyric_offset_ms")
            ):
                return None
            root = self.store.media / f"{ancestor['id']}-auto-alignment"
            for attempt in sorted(root.glob("attempt-*/attempt.json"), reverse=True):
                try:
                    info = json.loads(attempt.read_text(encoding="utf-8"))
                    if info.get("outcome") != "rendered" or info.get(
                        "source_file"
                    ) != ancestor.get("source_file"):
                        continue
                    for name in ("pipeline/subtitles", "provider_subtitles"):
                        folder = attempt.parent / name
                        if all(
                            (folder / f).is_file()
                            for f in (
                                "timeline.json",
                                "display_plan.json",
                                "style.json",
                            )
                        ):
                            return folder
                except (OSError, ValueError, TypeError):
                    continue
        return None

    @staticmethod
    def overlay_archive(bundle: Path) -> Path:
        return bundle.parent.parent if bundle.name == "subtitles" else bundle.parent

    def overlay_source(self, song: dict) -> Path | None:
        """Choose the source or its bitrate-capped cache for HTML subtitles."""
        if not self.store.settings()["tv_html_overlay"]:
            return None
        source_name = self.store.replay_assets(song).get("source_file")
        source = self.store.media / Path(source_name or "").name
        bundle = self.overlay_bundle(song)
        if (
            not source.is_file()
            or not bundle
            or not float(song.get("duration") or 0) > 0
        ):
            return None
        limit = self.store.settings()["max_bandwidth_mbps"]
        if source.stat().st_size * 8 > float(song["duration"] or 0) * limit * 800_000:
            archive = self.overlay_archive(bundle)
            cache = archive / "tv-source.mp4"
            if not cache.is_file():
                return None
            try:
                if cache.stat().st_size < 1024:
                    return None
                metadata = json.loads((archive / "attempt.json").read_text())
                if (
                    metadata.get("tv_source_ready") is False
                    or float(metadata.get("max_bandwidth_mbps", 0)) > limit
                ):
                    return None
            except (OSError, ValueError, TypeError):
                return None
            return cache
        return source

    def overlay_view(self, song: dict) -> dict | None:
        if self.overlay_source(song) is None:
            return None
        media_token = quote(
            self.auth.sign("media", f"{self.events.active_id}:{song['id']}", 8 * 3600)
        )
        tv_token = quote(
            self.auth.sign("overlay", f"{self.events.active_id}:{song['id']}", 8 * 3600)
        )
        return {
            "source_url": f"/media/{song['id']}/source?token={media_token}",
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
        info["media_url"] = self.signed_media(info)
        info["html_overlay"] = self.overlay_view(info)
        return info

    def state_with_active(self) -> dict:
        # The middleware pins the event. Only the active Store mutex is needed
        # so a close request may begin its transition while readers finish.
        with self.events.active().lock:
            return {**self.store.state(), "active": self.active_view()}
