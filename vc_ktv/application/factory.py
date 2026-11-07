"""FastAPI composition root: assemble stores, workers, auth and domain routers."""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import sqlite3
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote

from fastapi import Cookie, FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import app as legacy
from ..api.admin_accounts import create_admin_accounts_router
from ..api.admin_session import create_admin_session_router
from ..api.cancellation import create_cancellation_router
from ..api.download import create_download_router
from ..api.event_admin import EventCreate as EventCreate
from ..api.event_admin import PlaybackSettings as PlaybackSettings
from ..api.event_admin import create_event_admin_router
from ..api.event_history import create_event_history_router
from ..api.guest_auth import JoinEvent as JoinEvent
from ..api.guest_auth import RegisterUser as RegisterUser
from ..api.guest_auth import UserLogin as UserLogin
from ..api.guest_auth import create_guest_auth_router
from ..api.history import create_history_router
from ..api.lyrics import create_lyrics_router
from ..api.media import create_media_router
from ..api.netease_admin import create_netease_admin_router
from ..api.playback import create_playback_router
from ..api.preparation_admin import create_preparation_admin_router
from ..api.song_sources import (
    create_song_source_router,
)
from ..api.tv import create_tv_router
from ..autoprep import AutoPrep
from ..core.store import Store, time_now
from ..disk_budget import DiskBudgetExceeded
from ..events import CurrentStore, Events
from ..lyric_selection import netease_song_lyrics
from ..media_pins import MediaPins
from ..netease_catalog import lookup_netease_song
from ..netease_discovery import resolve_song_reference, search_netease_songs
from ..netease_login import NeteaseLogin
from ..process_cancel import terminate_thread_children
from .common import (
    MAX_ACTIVE_SONGS,
    MAX_USER_SONGS,
    STATIC,
    PinnedFileResponse,
    bail,
    bilibili_lookup,
    get_bvid,
    probe_media,
    vcpedia_find,
)


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
    netease_login = NeteaseLogin(
        storage_path=root / ".private" / "netease-session.json"
    )
    worker.netease_cookies = netease_login.cookies
    running = False

    async def monitor_workers():
        # Restart missing workers without requiring a service restart or a
        # second scheduler. Failed SQLite recovery is retried by start().
        while True:
            await asyncio.sleep(10)
            with events.lock:
                if not running or not events.active_id or events.transitioning:
                    continue
                live = sum(thread.is_alive() for thread in worker.workers)
                if live >= worker.max_workers + worker.fast_workers:
                    continue
                try:
                    if live:
                        worker.resume_after_failed_stop()
                    else:
                        worker.start()
                except Exception:
                    logging.getLogger(__name__).exception("AutoPrep recovery failed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal running
        if auto_prepare:
            from ..pipeline import primary_font_warning

            warning = primary_font_warning("Noto Sans CJK SC")
            if warning:
                logging.getLogger(__name__).warning("KTV subtitle font: %s", warning)
        running = True
        if auto_prepare and events.active_id:
            worker.store = (
                events.active()
            )  # Also handle events created before lifespan.
            worker.start()
        monitor = asyncio.create_task(monitor_workers()) if auto_prepare else None
        try:
            yield
        finally:
            running = False
            if monitor:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            if auto_prepare and worker.worker:
                worker.stop()
            netease_login.close()

    app = FastAPI(title="VC KTV MVP", version="0.5.0", lifespan=lifespan)
    app.state.autoprep = worker
    app.state.media_pins = media_pins
    app.state.store = store
    app.state.events = events
    app.state.lookup = bilibili_lookup
    app.state.netease_lookup = lookup_netease_song
    app.state.netease_resolve = resolve_song_reference
    app.state.netease_search = search_netease_songs
    app.state.netease_login = netease_login
    app.state.lyrics_lookup = vcpedia_find
    app.state.candidate_search = legacy.search_lyric_candidates
    app.state.netease_lyrics = netease_song_lyrics
    lyric_search_times = {}
    failed_login = {}
    failed_invites = {}
    failed_users = {}
    trusted_proxy_ips = {
        str(ipaddress.ip_address(value.strip()))
        for value in os.getenv("VC_KTV_TRUSTED_PROXY_IPS", "").split(",")
        if value.strip()
    }

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if peer in trusted_proxy_ips:
            # The trusted proxy MUST overwrite X-Real-IP, not forward a user
            # supplied value. Never trust forwarding headers from other peers.
            forwarded = request.headers.get("x-real-ip", "")
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return peer

    def pinned_media_response(path: Path, **kwargs) -> PinnedFileResponse:
        # Caller holds store.lock while choosing the path and making this hard
        # link. FileResponse opens the file only after its route returns.
        pinned = media_pins.pin(path)
        return PinnedFileResponse(pinned, **kwargs)

    def check_room_capacity():
        count = store.db.execute(
            "SELECT COUNT(*) FROM songs WHERE status IN ('rendering','ready','playing')"
            " OR (status='waiting' AND NOT "
            "(prep_state='unavailable' AND prep_attempts>=3))"
        ).fetchone()[0]
        if count >= MAX_ACTIVE_SONGS:
            bail(409, "本场待处理歌曲已满，请稍后再点歌")
        try:
            store.disk_budget.check()
        except DiskBudgetExceeded as error:
            bail(507, str(error))

    def check_guest_capacity(guest_id: str):
        count = store.db.execute(
            "SELECT COUNT(*) FROM songs WHERE requester=? AND "
            "(status IN ('rendering','ready','playing') OR "
            "(status='waiting' AND NOT "
            "(prep_state='unavailable' AND prep_attempts>=3)))",
            (guest_id,),
        ).fetchone()[0]
        if count >= MAX_USER_SONGS:
            bail(409, "每人最多保留二十首待唱歌曲")

    def check_new_order(guest_id: str):
        if store.settings()["orders_paused"]:
            bail(409, "本场已暂停点歌")
        check_room_capacity()
        check_guest_capacity(guest_id)

    app.state.probe = probe_media
    render_slot = threading.BoundedSemaphore(1)
    rendering = threading.Event()
    manual_render_threads: dict[int, int] = {}
    manual_render_lock = threading.RLock()

    def stop_manual_renders() -> None:
        with manual_render_lock:
            for tid in list(manual_render_threads.values()):
                terminate_thread_children(tid)

    worker.cancel_external_tasks = stop_manual_renders

    def stop_song_processing(song_id: int) -> None:
        worker.cancel_song(song_id)
        with manual_render_lock:
            if tid := manual_render_threads.get(song_id):
                terminate_thread_children(tid)

    def sign(kind: str, value: str, ttl: int) -> str:
        expiry = str(time_now() + ttl)
        payload = f"{kind}.{value}.{expiry}"
        sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + sig

    def read_signed(token: str | None, kind: str) -> str | None:
        if not token:
            return None
        try:
            k, value, exp, mac = token.split(".")
            msg = f"{k}.{value}.{exp}"
            correct = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()
            if (
                k == kind
                and int(exp) >= time_now()
                and hmac.compare_digest(mac, correct)
            ):
                return value
        except (ValueError, TypeError):
            pass
        return None

    def require_guest(token):
        raw = read_signed(token, "guest")
        prefix = events.active_id + ":" if events.active_id else ""
        return raw[len(prefix) :] if raw and prefix and raw.startswith(prefix) else None

    def current_user(token):
        guest = require_guest(token)
        if not guest or not events.active_id:
            return None
        with store.lock:
            row = store.db.execute(
                "SELECT g.id,g.name FROM users u JOIN guests g ON g.id=u.id WHERE u.id=?",
                (guest,),
            ).fetchone()
        return dict(row) if row else None

    def require_user(token):
        user = current_user(token)
        if not user:
            bail(401, "请先加入本场 KTV 并登录")
        return user

    def require_entry(token):
        if not events.active_id or read_signed(token, "entry") != events.active_id:
            bail(401, "请先输入本场六位加入码")

    def user_cookie(response, user_id, event_id):
        ttl = 7 * 86400
        response.set_cookie(
            "vc_guest",
            sign("guest", f"{event_id}:{user_id}", ttl),
            max_age=ttl,
            httponly=True,
            samesite="strict",
            path="/",
        )

    def admin_principal(token):
        """Validate scope and current account revision on every request."""
        identity = read_signed(token, "admin")
        if identity == "yes":
            return {"role": "global"}
        if not identity or not events.active_id:
            return None
        parts = identity.split(":")
        if len(parts) != 3 or parts[0] != events.active_id:
            return None
        event_id, account_id, revision = parts
        with store.lock:
            row = store.db.execute(
                "SELECT id,username,revision FROM event_admins WHERE id=?",
                (account_id,),
            ).fetchone()
        if row is None or str(row["revision"]) != revision:
            return None
        return {
            "role": "event",
            "event_id": event_id,
            "id": account_id,
            "username": row["username"],
            "revision": row["revision"],
        }

    def require_admin(token):
        principal = admin_principal(token)
        if principal is None:
            bail(401, "需要管理员登录")
        return principal

    def require_global_admin(token):
        principal = require_admin(token)
        if principal["role"] != "global":
            bail(403, "仅全局管理员可操作")
        return principal

    def signed_media(song: dict) -> str:
        return f"/media/{song['id']}?token={quote(sign('media', f'{events.active_id}:{song["id"]}', 8 * 3600))}"

    def overlay_bundle(song: dict):
        """Use the nearest matching subtitle archive along replay_of lineage."""
        if song.get("prep_state") != "done" or song.get("skip_subtitles"):
            return None
        resolved = store.replay_assets(song)
        if not resolved.get("source_file") or not resolved.get("media_file"):
            return None
        source = store.media / Path(resolved["source_file"]).name
        video = store.media / Path(resolved["media_file"]).name
        for ancestor in store.replay_chain(song):
            # An independently uploaded/re-rendered replay is not compatible
            # with its parent's subtitle plan. Legacy hardlinks, however,
            # remain valid when both physical files are still identical.
            for field, actual in (("source_file", source), ("media_file", video)):
                path = ancestor.get(field)
                if path:
                    try:
                        if not (store.media / Path(path).name).samefile(actual):
                            return None
                    except OSError:
                        return None
            if ancestor["id"] != song["id"] and any(
                ancestor.get(field) != song.get(field)
                for field in ("lyrics", "lyric_choice", "lyric_offset_ms")
            ):
                return None
            root = store.media / f"{ancestor['id']}-auto-alignment"
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

    def overlay_archive(bundle: Path) -> Path:
        return bundle.parent.parent if bundle.name == "subtitles" else bundle.parent

    def overlay_view(song: dict):
        if not store.settings()["tv_html_overlay"]:
            return None
        source_name = store.replay_assets(song).get("source_file")
        source = store.media / Path(source_name or "").name
        bundle = overlay_bundle(song)
        if (
            not source.is_file()
            or not bundle
            or not float(song.get("duration") or 0) > 0
        ):
            return None
        # Never stream a high-bitrate source over the 4 Mbps proxy by accident.
        limit = store.settings()["max_bandwidth_mbps"]
        if source.stat().st_size * 8 > float(song["duration"] or 0) * limit * 800_000:
            cache = overlay_archive(bundle) / "tv-source.mp4"
            if not cache.is_file():
                return None
            try:
                metadata = json.loads(
                    (overlay_archive(bundle) / "attempt.json").read_text()
                )
                if float(metadata.get("max_bandwidth_mbps", 0)) > limit:
                    return None
            except (OSError, ValueError, TypeError):
                return None
        media_token = quote(sign("media", f"{events.active_id}:{song['id']}", 8 * 3600))
        tv_token = quote(sign("overlay", f"{events.active_id}:{song['id']}", 8 * 3600))
        return {
            "source_url": f"/media/{song['id']}/source?token={media_token}",
            "timeline_url": f"/api/tv/subtitles/{song['id']}?token={tv_token}",
        }

    def active_view():
        song = store.current()
        if not song:
            return None
        info = store.rows(
            """SELECT s.*, g.name AS nickname FROM songs s
                             JOIN guests g ON g.id=s.requester WHERE s.id=?""",
            (song["id"],),
        )[0]
        info = store.replay_assets(info)
        info["media_url"] = signed_media(info)
        info["html_overlay"] = overlay_view(info)
        return info

    def state_with_active():
        # The middleware pins the event; use only its Store mutex here so
        # an event-close request can set transitioning while readers drain.
        with events.active().lock:
            return {**store.state(), "active": active_view()}

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

    def archived_guest_event(token: str | None):
        identity = read_signed(token, "guest")
        if not identity or ":" not in identity:
            return None
        event_id, user_id = identity.split(":", 1)
        if event_id == events.active_id:
            return None
        with events.lock:
            event = next(
                (
                    e
                    for e in events.list()
                    if e["id"] == event_id and e["status"] == "closed"
                ),
                None,
            )
            if not event:
                return None
            try:
                path = events.database_path(event_id)
                with sqlite3.connect(
                    f"file:{quote(str(path), safe='/')}?mode=ro", uri=True
                ) as db:
                    row = db.execute(
                        "SELECT username FROM users WHERE id=?", (user_id,)
                    ).fetchone()
            except sqlite3.Error:
                return None
            return (
                {"id": event_id, "name": event["name"], "username": row[0]}
                if row
                else None
            )

    app.include_router(
        create_guest_auth_router(
            events=events,
            store=store,
            current_user=current_user,
            read_signed=read_signed,
            archived_guest_event=archived_guest_event,
            require_entry=require_entry,
            client_ip=client_ip,
            sign=sign,
            user_cookie=user_cookie,
            failed_invites=failed_invites,
            failed_users=failed_users,
        )
    )

    app.include_router(create_netease_admin_router(netease_login, require_global_admin))

    event_history_router, export_event_songs = create_event_history_router(
        events, require_admin
    )
    app.include_router(event_history_router)

    def publish_worker(replacement: AutoPrep) -> None:
        nonlocal worker
        worker = replacement
        app.state.autoprep = replacement

    app.include_router(
        create_event_admin_router(
            store=store,
            events=events,
            require_admin=require_admin,
            require_global_admin=require_global_admin,
            export_event_songs=export_event_songs,
            get_worker=lambda: worker,
            publish_worker=publish_worker,
            probe_media=probe_media,
            netease_login=netease_login,
            stop_manual_renders=stop_manual_renders,
            is_running=lambda: running,
            auto_prepare=auto_prepare,
            rendering=rendering,
        )
    )

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "version": "0.5.0",
            "auto_prepare": auto_prepare,
            "autoprep_workers": sum(thread.is_alive() for thread in worker.workers),
            "gpu_alignment": gpu_alignment,
            "autoprep_ready": (
                auto_prepare
                and (
                    not events.active_id
                    or any(thread.is_alive() for thread in worker.workers)
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
        user = current_user(vc_guest)
        if user is None and admin_principal(vc_admin) is None:
            require_user(vc_guest)
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
            current_user=current_user,
            admin_principal=admin_principal,
            require_user=require_user,
            check_new_order=check_new_order,
        )
    )

    app.include_router(
        create_lyrics_router(
            app=app,
            store=store,
            events=events,
            require_user=require_user,
            require_admin=require_admin,
            gpu_alignment=gpu_alignment,
            sign=sign,
            read_signed=read_signed,
            lyric_search_times=lyric_search_times,
        )
    )

    app.include_router(create_admin_accounts_router(app, require_global_admin))

    app.include_router(
        create_admin_session_router(
            store=store,
            events=events,
            password=password,
            client_ip=client_ip,
            sign=sign,
            require_admin=require_admin,
            state_with_active=state_with_active,
            failed_login=failed_login,
        )
    )

    app.include_router(
        create_preparation_admin_router(
            app=app,
            store=store,
            events=events,
            require_admin=require_admin,
            check_room_capacity=check_room_capacity,
            check_guest_capacity=check_guest_capacity,
            render_slot=render_slot,
            rendering=rendering,
            manual_render_threads=manual_render_threads,
            manual_render_lock=manual_render_lock,
        )
    )

    playback_router, next_impl, finish_impl, schedule_replay = create_playback_router(
        store=store,
        require_admin=require_admin,
        require_user=require_user,
        active_view=active_view,
        check_new_order=check_new_order,
        check_room_capacity=check_room_capacity,
    )
    app.include_router(playback_router)

    app.include_router(
        create_tv_router(
            store=store,
            events=events,
            require_admin=require_admin,
            read_signed=read_signed,
            sign=sign,
            state_with_active=state_with_active,
            schedule_replay=schedule_replay,
            signed_media=signed_media,
            overlay_view=overlay_view,
            next_impl=next_impl,
            finish_impl=finish_impl,
        )
    )

    app.include_router(
        create_history_router(
            app,
            current_user=current_user,
            admin_principal=admin_principal,
            require_user=require_user,
            archived_guest_event=archived_guest_event,
        )
    )

    app.include_router(
        create_cancellation_router(
            store=store,
            require_admin=require_admin,
            require_user=require_user,
            stop_song_processing=stop_song_processing,
        )
    )

    app.include_router(
        create_download_router(
            store=store,
            events=events,
            admin_principal=admin_principal,
            require_user=require_user,
            pinned_media_response=pinned_media_response,
        )
    )

    app.include_router(
        create_media_router(
            store=store,
            events=events,
            read_signed=read_signed,
            overlay_view=overlay_view,
            overlay_archive=overlay_archive,
            overlay_bundle=overlay_bundle,
            pinned_media_response=pinned_media_response,
        )
    )

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_page():
        return FileResponse(STATIC / "index.html")

    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
    return app
