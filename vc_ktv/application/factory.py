"""FastAPI composition root: assemble stores, workers, auth and domain routers."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Cookie, FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from vc_ktv.api.admin_accounts import create_admin_accounts_router
from vc_ktv.api.admin_session import create_admin_session_router
from vc_ktv.api.cancellation import create_cancellation_router
from vc_ktv.api.download import create_download_router
from vc_ktv.api.event_admin import EventCreate as EventCreate
from vc_ktv.api.event_admin import PlaybackSettings as PlaybackSettings
from vc_ktv.api.event_admin import create_event_admin_router
from vc_ktv.api.event_history import create_event_history_router
from vc_ktv.api.guest_auth import JoinEvent as JoinEvent
from vc_ktv.api.guest_auth import RegisterUser as RegisterUser
from vc_ktv.api.guest_auth import UserLogin as UserLogin
from vc_ktv.api.guest_auth import create_guest_auth_router
from vc_ktv.api.history import create_history_router
from vc_ktv.api.lyrics import create_lyrics_router
from vc_ktv.api.media import create_media_router
from vc_ktv.api.netease_admin import create_netease_admin_router
from vc_ktv.api.playback import create_playback_router
from vc_ktv.api.preparation_admin import create_preparation_admin_router
from vc_ktv.api.song_sources import create_song_source_router
from vc_ktv.api.tv import create_tv_router
from vc_ktv.application.archive import EventArchiveService
from vc_ktv.application.auth import AuthService
from vc_ktv.application.common import (
    STATIC,
    bilibili_lookup,
    get_bvid,
    probe_media,
    vcpedia_find,
)
from vc_ktv.application.event_lifecycle import EventLifecycle
from vc_ktv.application.guest_lyrics import LyricSelectionService
from vc_ktv.application.media import MediaService
from vc_ktv.application.orders import OrderService
from vc_ktv.application.playback import PlaybackService
from vc_ktv.application.tv import TVSessionService
from vc_ktv.core.events import CurrentStore, Events
from vc_ktv.core.store import Store, time_now
from vc_ktv.infra.media_pins import MediaPins
from vc_ktv.lyrics.lyric_selection import netease_song_lyrics, search_lyric_candidates
from vc_ktv.processing.manual_render import ManualRenderManager
from vc_ktv.processing.worker import AutoPrep
from vc_ktv.sources.netease_catalog import lookup_netease_song
from vc_ktv.sources.netease_discovery import (
    resolve_song_reference,
    search_netease_songs,
)
from vc_ktv.sources.netease_login import NeteaseLogin


def create_app(
    root: Path | None = None,
    password: str | None = None,
    secret: bytes | None = None,
    auto_prepare: bool | None = None,
    gpu_alignment: bool | None = None,
) -> FastAPI:
    root = Path(root or os.getenv("VC_KTV_DATA", "~/.local/share/vc-ktv")).expanduser()
    config = Path(os.getenv("VC_KTV_CONFIG", "~/.config/vc-ktv")).expanduser()
    if password is None:
        f = config / "admin-password"
        if not f.exists():
            raise RuntimeError(f"Missing {f}; run scripts/setup.sh first")
        password = f.read_text().strip()
    if secret is None:
        f = config / "token-secret"
        if not f.exists():
            raise RuntimeError(f"Missing {f}; run scripts/setup.sh first")
        secret = bytes.fromhex(f.read_text().strip())
    events = Events(root, Store)
    store = CurrentStore(events)
    media_pins = MediaPins(root)
    if auto_prepare is None:
        auto_prepare = os.getenv("VC_KTV_AUTOPREPARE", "1") != "0"
    if gpu_alignment is None:
        gpu_alignment = os.getenv("VC_KTV_GPU_ALIGNMENT", "1") != "0"
    worker = AutoPrep(
        events.active() if events.active_id else store,
        probe_media,
        gpu_alignment=gpu_alignment,
    )
    auth = AuthService(events, store, password, secret)
    archive = EventArchiveService(events, auth)
    lyric_selection = LyricSelectionService(store, events, auth, gpu_alignment)
    media = MediaService(store, events, auth, media_pins)
    orders = OrderService(store, events)
    playback = PlaybackService(store, orders)
    tv = TVSessionService(store, events, auth)
    manual = ManualRenderManager(store, events)
    netease_login = NeteaseLogin(
        storage_path=root / ".private" / "netease-session.json"
    )
    worker.netease_cookies = netease_login.cookies
    worker.cancel_external_tasks = manual.stop_all
    lifecycle = EventLifecycle(
        events, worker, manual, netease_login, probe_media, auto_prepare
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if auto_prepare:
            from vc_ktv.pipeline import primary_font_warning

            warning = primary_font_warning("Noto Sans CJK SC")
            if warning:
                logging.getLogger(__name__).warning("KTV subtitle font: %s", warning)
        lifecycle.running = True
        if auto_prepare and events.active_id:
            lifecycle.worker.store = events.active()  # Pre-created event.
            lifecycle.worker.start()
        monitor = (
            asyncio.create_task(lifecycle.monitor_workers()) if auto_prepare else None
        )
        try:
            yield
        finally:
            lifecycle.running = False
            if monitor:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            if auto_prepare and lifecycle.worker.worker:
                lifecycle.worker.stop()
            netease_login.close()

    app = FastAPI(title="VC KTV MVP", version="0.5.0", lifespan=lifespan)
    app.state.auth = auth
    app.state.archive = archive
    app.state.lyric_selection = lyric_selection
    app.state.orders = orders
    app.state.playback = playback
    app.state.tv_session = tv
    app.state.manual_render = manual
    app.state.autoprep = lifecycle.worker
    app.state.lifecycle = lifecycle
    lifecycle.bind_state(app.state)
    app.state.media_pins = media_pins
    app.state.media = media
    app.state.store = store
    app.state.events = events
    app.state.lookup = bilibili_lookup
    app.state.netease_lookup = lookup_netease_song
    app.state.netease_resolve = resolve_song_reference
    app.state.netease_search = search_netease_songs
    app.state.netease_login = netease_login
    app.state.lyrics_lookup = vcpedia_find
    app.state.candidate_search = search_lyric_candidates
    app.state.netease_lyrics = netease_song_lyrics

    app.state.probe = probe_media

    @app.middleware("http")
    async def origin_check(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                from urllib.parse import urlparse

                parsed = urlparse(origin)
                if parsed.netloc != request.headers.get(
                    "host"
                ) or parsed.scheme not in ("http", "https"):
                    return Response(
                        status_code=403, content="Cross-site request denied"
                    )
        path = request.url.path
        # Closing/creating the event are writers, not readers. Other API and
        # media routes cannot switch Store halfway through a response.
        lifecycle_writer = request.method == "POST" and path in {
            "/api/admin/events",
            "/api/admin/events/close",
        }
        if (
            not (path.startswith("/api/") or path.startswith("/media/"))
            or lifecycle_writer
        ):
            return await call_next(request)
        if not events.begin_request():
            return Response("场次正在切换，请重试", status_code=409)
        try:
            return await call_next(request)
        finally:
            events.end_request()

    @app.get("/api/event")
    def current_event():
        meta = events.metadata()
        return {"event": {"id": meta["id"], "name": meta["name"]} if meta else None}

    app.include_router(create_guest_auth_router(auth=auth, store=store, events=events))

    app.include_router(
        create_netease_admin_router(netease_login, auth.require_global_admin)
    )

    app.include_router(create_event_history_router(archive))

    app.include_router(
        create_event_admin_router(
            store=store,
            events=events,
            require_admin=auth.require_admin,
            require_global_admin=auth.require_global_admin,
            archive=archive,
            lifecycle=lifecycle,
        )
    )

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "version": "0.5.0",
            "auto_prepare": auto_prepare,
            "autoprep_workers": sum(
                thread.is_alive() for thread in lifecycle.worker.workers
            ),
            "gpu_alignment": gpu_alignment,
            "autoprep_ready": (
                auto_prepare
                and (
                    not events.active_id
                    or any(thread.is_alive() for thread in lifecycle.worker.workers)
                )
            ),
        }

    @app.get("/api/state")
    def state(
        vc_guest: str | None = Cookie(default=None),
        vc_admin: str | None = Cookie(default=None),
    ):
        if not events.active_id:
            return {
                "current": None,
                "queue": [],
                "waiting": [],
                "guests": [],
                "server_time": time_now(),
            }
        user = auth.current_user(vc_guest)
        if user is None and auth.admin_principal(vc_admin) is None:
            auth.require_user(vc_guest)
        guest = user["id"] if user else None
        snapshot = store.state()
        for song in [*snapshot["queue"], *snapshot["waiting"]]:
            song["mine"] = bool(guest and song["requester_id"] == guest)
        if snapshot["current"]:
            snapshot["current"]["mine"] = bool(
                guest and snapshot["current"]["requester_id"] == guest
            )
        return snapshot

    app.include_router(
        create_song_source_router(
            app,
            get_bvid=get_bvid,
            current_user=auth.current_user,
            admin_principal=auth.admin_principal,
            require_user=auth.require_user,
            orders=orders,
        )
    )

    app.include_router(create_lyrics_router(app=app, lyrics=lyric_selection))

    app.include_router(create_admin_accounts_router(app, auth.require_global_admin))

    app.include_router(
        create_admin_session_router(auth=auth, events=events, media=media)
    )

    app.include_router(
        create_preparation_admin_router(
            app=app,
            store=store,
            events=events,
            require_admin=auth.require_admin,
            check_room_capacity=orders.check_room_capacity,
            check_guest_capacity=orders.check_guest_capacity,
            manual=manual,
        )
    )

    app.include_router(
        create_playback_router(
            playback=playback,
            require_admin=auth.require_admin,
            require_user=auth.require_user,
            media=media,
        )
    )

    app.include_router(create_tv_router(tv=tv, playback=playback, media=media))

    app.include_router(
        create_history_router(
            app,
            current_user=auth.current_user,
            admin_principal=auth.admin_principal,
            require_user=auth.require_user,
            archived_guest_event=auth.archived_guest_event,
        )
    )

    app.include_router(
        create_cancellation_router(
            store=store,
            require_admin=auth.require_admin,
            require_user=auth.require_user,
            stop_song_processing=lifecycle.stop_song,
        )
    )

    app.include_router(
        create_download_router(
            store=store,
            events=events,
            admin_principal=auth.admin_principal,
            require_user=auth.require_user,
            media=media,
        )
    )

    app.include_router(create_media_router(media=media, auth=auth))

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_page():
        return FileResponse(STATIC / "index.html")

    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
    return app
