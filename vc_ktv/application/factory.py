"""FastAPI composition root: assemble stores, workers, auth and domain routers."""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Cookie, FastAPI, File, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .. import app as legacy
from ..accounts import verify_password
from ..api.admin_accounts import create_admin_accounts_router
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
from ..api.song_sources import (
    create_song_source_router,
)
from ..api.tv import create_tv_router
from ..autoprep import AutoPrep
from ..core.store import Store, time_now
from ..disk_budget import DiskBudgetExceeded
from ..events import CurrentStore, Events
from ..karaoke import write_ass
from ..lyric_selection import netease_song_lyrics
from ..media_pins import MediaPins
from ..netease_catalog import lookup_netease_song
from ..netease_discovery import resolve_song_reference, search_netease_songs
from ..netease_login import NeteaseLogin
from ..process_cancel import terminate_thread_children
from ..textgrid_import import convert_textgrid
from .common import (
    MAX_ACTIVE_SONGS,
    MAX_USER_SONGS,
    MEDIA_MIMES,
    STATIC,
    PinnedFileResponse,
    bail,
    bilibili_lookup,
    get_bvid,
    probe_media,
    vcpedia_find,
)
from .schemas import (
    Finish,
    Login,
    Lyrics,
    PlaybackMode,
    RenderRequest,
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

    @app.post("/api/admin/login")
    def login(body: Login, request: Request, response: Response):
        ip = client_ip(request)
        key = (ip, body.username.strip() if body.username else "")
        recent = [t for t in failed_login.get(key, []) if t > time_now() - 300]
        if len(recent) >= 10:
            bail(429, "登录尝试过于频繁，请稍后重试")
        identity = None
        if body.username is None or not body.username.strip():
            if hmac.compare_digest(body.password, password):
                identity = "yes"
        elif events.active_id:
            with store.lock:
                account = store.db.execute(
                    "SELECT id,password_hash,revision FROM event_admins WHERE username=?",
                    (body.username.strip(),),
                ).fetchone()
                if (
                    account
                    and len(body.password) <= 72
                    and verify_password(body.password, account["password_hash"])
                ):
                    identity = (
                        f"{events.active_id}:{account['id']}:{account['revision']}"
                    )
        if identity is None:
            recent.append(time_now())
            failed_login[key] = recent
            bail(401, "用户名或密码错误")
        failed_login.pop(key, None)
        response.set_cookie(
            "vc_admin",
            sign("admin", identity, 7 * 86400),
            max_age=7 * 86400,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return {"ok": True}

    @app.post("/api/admin/logout")
    def logout(response: Response):
        response.delete_cookie("vc_admin", path="/")
        return {"ok": True}

    @app.get("/api/admin/state")
    def admin_state(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        if not events.active_id:
            return {
                "current": None,
                "queue": [],
                "waiting": [],
                "guests": [],
                "active": None,
            }
        return state_with_active()

    @app.post("/api/admin/songs/{song_id}/subtitles/retry")
    def retry_subtitles(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["status"] != "ready" or row["prep_state"] not in (
                "original",
                "done",
            ):
                bail(409, "仅可重制尚未播放且字幕已完成或失败的歌曲")
            source_file = store.replay_assets(row).get("source_file")
            if not row["lyric_choice"] or not source_file:
                bail(409, "歌曲缺少已选歌词或原始视频")
            if not (store.media / Path(source_file).name).is_file():
                bail(409, "原视频已丢失，无法仅重试字幕")
            try:
                store.db.execute(
                    "UPDATE songs SET prep_state='downloaded',prep_error='',"
                    "source_file=?,skip_subtitles=0,prep_stage='queued',prep_stage_at=? WHERE id=?",
                    (source_file, time_now(), song_id),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "status": "downloaded"}

    @app.post("/api/admin/songs/{song_id}/download/retry")
    def retry_media_download(song_id: int, vc_admin: str | None = Cookie(default=None)):
        """Requeue one failed download without requiring the guest to re-order."""
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT status,prep_state,prep_attempts,requester "
                "FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if row is None:
                bail(404, "歌曲不存在")
            if row["status"] != "waiting" or row["prep_state"] != "unavailable":
                bail(409, "只有准备失败的待处理歌曲可以重试下载")
            # A terminal failure frees its capacity; manually restarting it
            # must claim capacity again, without being subject to order pause.
            if row["prep_attempts"] >= 3:
                check_room_capacity()
                check_guest_capacity(row["requester"])
            try:
                store.db.execute(
                    "UPDATE songs SET prep_state='pending',prep_attempts=0,"
                    "prep_retry_at=0,prep_error='',prep_stage='download' WHERE id=?",
                    (song_id,),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id}

    @app.post("/api/admin/songs/{song_id}/subtitles/skip")
    def skip_subtitles(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT status,prep_state,source_file,skip_subtitles "
                "FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if (
                row["skip_subtitles"]
                or row["status"] != "ready"
                or row["prep_state"]
                not in (
                    "downloaded",
                    "original",
                    "aligning",
                )
            ):
                bail(409, "仅可对已下载且尚未播放的歌曲跳过字幕")
            source = store.media / Path(row["source_file"] or "").name
            if not source.is_file():
                bail(409, "原视频尚未就绪，无法进入队列")
            try:
                store.db.execute(
                    "UPDATE songs SET media_file=source_file,prep_state='original',"
                    "skip_subtitles=1,prep_stage='skipped',prep_stage_at=?,"
                    "prep_error='' WHERE id=?",
                    (time_now(), song_id),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "status": "ready", "source": "original"}

    @app.post("/api/admin/songs/{song_id}/media")
    async def upload(
        song_id: int,
        file: Annotated[UploadFile, File()],
        vc_admin: str | None = Cookie(default=None),
    ):
        require_admin(vc_admin)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in MEDIA_MIMES:
            bail(400, "仅支持 MP4、M4V、WebM 文件")
        with store.lock:
            row = store.db.execute(
                "SELECT status,media_file,source_file FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["status"] not in ("waiting", "ready"):
                bail(409, "正在播放或已结束，不能覆盖视频")
        target = store.media / f"{song_id}-{secrets.token_hex(8)}{suffix}"
        size = 0
        try:
            with target.open("wb") as out:
                while block := await file.read(1024 * 1024):
                    size += len(block)
                    out.write(block)
                    # Include this file and all existing room artifacts in
                    # the aggregate 200 GiB budget, not just this upload.
                    if size % (16 * 1024 * 1024) < len(block):
                        try:
                            store.disk_budget.check()
                        except DiskBudgetExceeded as error:
                            bail(507, str(error))
            try:
                store.disk_budget.check()
            except DiskBudgetExceeded as error:
                bail(507, str(error))
            if not size:
                bail(400, "视频为空")
            try:
                actual_duration = await run_in_threadpool(app.state.probe, target)
            except ValueError as error:
                bail(400, str(error))
            with store.lock:
                row = store.db.execute(
                    "SELECT status,media_file,source_file FROM songs WHERE id=?",
                    (song_id,),
                ).fetchone()
                if row["status"] not in ("waiting", "ready"):
                    bail(409, "歌曲状态已变化")
                try:
                    store.db.execute(
                        "UPDATE songs SET status='ready',media_file=?,source_file=?,duration=?,render_error='',prep_state='manual',prep_error='' WHERE id=?",
                        (target.name, target.name, round(actual_duration), song_id),
                    )
                    store.db.commit()
                except BaseException:
                    # The outer handler must see the last committed media
                    # pointer before deciding whether to remove this upload.
                    store.db.rollback()
                    raise
            for old in set(filter(None, (row["media_file"], row["source_file"]))):
                if old != target.name:
                    try:
                        (store.media / old).unlink(missing_ok=True)
                    except OSError as error:
                        logging.getLogger(__name__).warning(
                            "Song #%s obsolete upload cleanup failed: %s",
                            song_id,
                            error,
                        )
        except BaseException:
            with store.lock:
                if not store.db.execute(
                    "SELECT 1 FROM songs WHERE id=? AND media_file=?",
                    (song_id, target.name),
                ).fetchone():
                    target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        return {"id": song_id, "status": "ready", "bytes": size}

    @app.post("/api/admin/songs/{song_id}/lyrics")
    def set_lyrics(
        song_id: int, body: Lyrics, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        with store.lock:
            if not store.db.execute(
                "SELECT 1 FROM songs WHERE id=?", (song_id,)
            ).fetchone():
                bail(404, "歌曲不存在")
            if (
                store.db.execute(
                    "SELECT status FROM songs WHERE id=?", (song_id,)
                ).fetchone()[0]
                == "rendering"
            ):
                bail(409, "字幕制作中暂不能修改歌词")
            try:
                store.db.execute(
                    "UPDATE songs SET lyrics=? WHERE id=?", (body.text, song_id)
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True}

    @app.get("/api/admin/songs/{song_id}/lyrics")
    def get_lyrics(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            row = store.db.execute(
                "SELECT lyrics FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            return {"text": row[0]}

    def render_worker(
        target_store: Store,
        song_id: int,
        source: Path,
        lyrics: str,
        duration: float,
        lead: float,
    ):
        destination = target_store.media / f"{song_id}-ktv-{secrets.token_hex(8)}.mp4"
        with manual_render_lock:
            manual_render_threads[song_id] = threading.get_native_id()
        try:
            with target_store.lock:
                current = target_store.db.execute(
                    "SELECT status FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if not current or current["status"] != "rendering":
                    return
            legacy.render_video(source, lyrics, destination, duration, lead)
            with target_store.lock:
                row = target_store.db.execute(
                    "SELECT media_file,status FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if row and row["status"] == "rendering":
                    try:
                        target_store.db.execute(
                            "UPDATE songs SET status='ready',media_file=?,render_error='' WHERE id=?",
                            (destination.name, song_id),
                        )
                        target_store.db.commit()
                    except BaseException:
                        # The outer handler deletes destination. Roll back the
                        # media pointer before it records the render failure.
                        target_store.db.rollback()
                        raise
                    # Publishing is complete; deleting the old video is only
                    # best effort and must never invalidate the new DB path.
                    if row["media_file"] and row["media_file"] != source.name:
                        try:
                            (target_store.media / row["media_file"]).unlink(
                                missing_ok=True
                            )
                        except OSError as error:
                            logging.getLogger(__name__).warning(
                                "Song #%s obsolete manual render cleanup failed: %s",
                                song_id,
                                error,
                            )
                else:
                    destination.unlink(missing_ok=True)
        except Exception as error:
            destination.unlink(missing_ok=True)
            with target_store.lock:
                for attempt in range(2):
                    try:
                        target_store.db.execute(
                            "UPDATE songs SET status='ready',render_error=? WHERE id=? AND status='rendering'",
                            (f"制作失败：{type(error).__name__}", song_id),
                        )
                        target_store.db.commit()
                        break
                    except sqlite3.Error:
                        target_store.db.rollback()
                        logging.getLogger(__name__).exception(
                            "Manual render failure writeback failed for song #%s (attempt %s)",
                            song_id,
                            attempt + 1,
                        )
        finally:
            with manual_render_lock:
                manual_render_threads.pop(song_id, None)
            rendering.clear()
            render_slot.release()

    @app.post("/api/admin/songs/{song_id}/alignment/import")
    async def import_textgrid(
        song_id: int,
        file: Annotated[UploadFile, File()],
        offset: float = 0,
        vc_admin: str | None = Cookie(default=None),
    ):
        require_admin(vc_admin)
        if not (file.filename or "").lower().endswith(".textgrid"):
            bail(400, "请上传 TIFA TextGrid 文件")
        content = await file.read()
        await file.close()
        with store.lock:
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["status"] not in ("ready", "waiting") or not store.replay_assets(
                row
            ).get("source_file"):
                bail(409, "请先上传原始视频，且不能在播放或制作过程中替换时间轴")
            original = row["lyrics"]
            duration = row["duration"]
        try:
            with tempfile.TemporaryDirectory(prefix="vc-ktv-grid-") as temp:
                path = Path(temp) / "alignment.TextGrid"
                path.write_bytes(content)
                proposed, report = convert_textgrid(path, original, duration, offset)
                write_ass(proposed, Path(temp) / "validated.ass", duration, 1.8)
        except (ValueError, UnicodeError, OSError, AssertionError) as error:
            bail(400, f"TextGrid 无法安全导入：{error}")
        # Keep raw alignment and original text for reproducibility.
        archive = store.media / f"{song_id}-alignment-{secrets.token_hex(5)}"
        archive.mkdir(mode=0o700)
        try:
            (archive / "alignment.TextGrid").write_bytes(content)
            (archive / "lyrics.before.txt").write_text(original, encoding="utf-8")
            (archive / "lyrics.enhanced.lrc").write_text(proposed, encoding="utf-8")
            with store.lock:
                latest = store.db.execute(
                    "SELECT status,lyrics FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if (
                    not latest
                    or latest["status"] not in ("ready", "waiting")
                    or latest["lyrics"] != original
                ):
                    bail(409, "歌词或歌曲状态发生变化，请重新导入")
                try:
                    store.db.execute(
                        "UPDATE songs SET lyrics=? WHERE id=?", (proposed, song_id)
                    )
                    store.db.commit()
                except sqlite3.Error:
                    store.db.rollback()
                    raise
        except BaseException:
            import shutil

            shutil.rmtree(archive)
            raise
        return {"id": song_id, "report": report, "lyrics": proposed}

    @app.post("/api/admin/songs/{song_id}/render")
    def render_song(
        song_id: int, body: RenderRequest, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        if not render_slot.acquire(blocking=False):
            bail(409, "已有歌曲正在制作，请稍后再试")
        try:
            with store.lock:
                row = store.db.execute(
                    "SELECT * FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if not row:
                    bail(404, "歌曲不存在")
                asset = store.replay_assets(row)
                if row["status"] != "ready" or not asset.get("source_file"):
                    bail(409, "请先上传可用视频")
                source = store.media / Path(asset["source_file"]).name
                if not source.is_file():
                    bail(404, "原视频不存在，请重新上传")
                try:
                    with tempfile.TemporaryDirectory(prefix="vc-ktv-validate-") as tmp:
                        write_ass(
                            row["lyrics"],
                            Path(tmp) / "lyrics.ass",
                            row["duration"],
                            body.lead,
                        )
                except ValueError as error:
                    bail(400, str(error))
                try:
                    store.db.execute(
                        "UPDATE songs SET status='rendering',render_error='' WHERE id=?",
                        (song_id,),
                    )
                    store.db.commit()
                except BaseException:
                    store.db.rollback()
                    raise
                rendering.set()
                try:
                    render_thread = threading.Thread(
                        target=render_worker,
                        args=(
                            events.active(),
                            song_id,
                            source,
                            row["lyrics"],
                            row["duration"],
                            body.lead,
                        ),
                        daemon=True,
                    )
                    render_thread.start()
                except BaseException:
                    # No worker was launched to clear rendering or release the
                    # slot. Restore the state before returning an error.
                    rendering.clear()
                    store.db.execute(
                        "UPDATE songs SET status='ready' WHERE id=? AND status='rendering'",
                        (song_id,),
                    )
                    store.db.commit()
                    raise
                return {"id": song_id, "status": "rendering", "lead": body.lead}
        except BaseException:
            render_slot.release()
            raise

    def next_impl():
        with store.lock:
            store.claim_next()
            return {"active": active_view()}

    def finish_impl(body: Finish):
        with store.lock:
            next_ready = store.finish_current(
                body.song_id, skipped=body.reason == "skip"
            )
            if next_ready is None:
                bail(409, "当前播放已变化")
            return {"ok": True, "finished": body.song_id, "next_ready": next_ready}

    @app.put("/api/admin/playback-mode")
    def change_playback_mode(
        body: PlaybackMode, vc_admin: str | None = Cookie(default=None)
    ):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() != body.mode and store.current():
                bail(409, "请先唱完或跳过当前歌曲，再切换播放模式")
            try:
                store.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('playback_mode',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (body.mode,),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"mode": body.mode}

    @app.get("/api/admin/played")
    def recently_played(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            return {
                "songs": store.rows(
                    """SELECT s.id,s.bvid,s.source_type,s.source_id,s.title,s.duration,
                          s.page,s.played_at,s.replay_of,g.name AS nickname
                   FROM songs s JOIN guests g ON g.id=s.requester
                   WHERE s.status='played' ORDER BY s.played_at DESC,s.id DESC
                   LIMIT 50"""
                )
            }

    @app.post("/api/admin/songs/{song_id}/pin-next")
    def pin_next_song(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            song = next((s for s in store.songs() if s["id"] == song_id), None)
            if not song:
                if not store.db.execute(
                    "SELECT 1 FROM songs WHERE id=?", (song_id,)
                ).fetchone():
                    bail(404, "歌曲不存在")
                bail(409, "只能置顶尚未播放且已可播放的歌曲")
            if not store.playable(song, store.settings()):
                bail(409, "歌曲尚未准备好，不能安排为下一首")
            # A single next-song slot: replacing an earlier pin or replay is atomic.
            try:
                store.db.execute(
                    "UPDATE songs SET priority_next=0 WHERE priority_next=1"
                )
                store.db.execute(
                    "UPDATE songs SET priority_next=1 WHERE id=?", (song_id,)
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"ok": True, "id": song_id, "priority_next": True}

    def schedule_replay(song_id: int, *, current_only=False, guest_id=None):
        # A guest replay is a new ordinary fair-turn request. Admin/TV
        # replays preserve their existing next-song priority behavior.
        with store.lock:
            if guest_id is not None:
                check_new_order(guest_id)
            if current_only and (
                not store.current() or store.current()["id"] != song_id
            ):
                bail(409, "当前歌曲已发生变化")
            row = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if (
                row["status"]
                not in (
                    ("playing",)
                    if current_only
                    else ("played", "skipped")
                    if guest_id is not None
                    else ("played",)
                )
                or row["prep_state"] not in ("done", "manual", "original")
                or not store.replay_assets(row).get("media_file")
            ):
                bail(409, "只能重唱当前正在播放或本场已唱完的成片")
            if current_only:
                # Repeated TV taps must never create multiple replay requests.
                existing = store.db.execute(
                    "SELECT id FROM songs WHERE replay_of=? AND status='ready' ORDER BY id LIMIT 1",
                    (song_id,),
                ).fetchone()
                if existing:
                    try:
                        store.db.execute(
                            "UPDATE songs SET priority_next=0 WHERE priority_next=1"
                        )
                        store.db.execute(
                            "UPDATE songs SET priority_next=1 WHERE id=?",
                            (existing["id"],),
                        )
                        store.db.commit()
                    except sqlite3.Error:
                        store.db.rollback()
                        raise
                    return {
                        "id": existing["id"],
                        "status": "ready",
                        "priority_next": True,
                    }
            asset = store.replay_assets(row)
            media = store.media / Path(asset.get("media_file") or "").name
            if not media.is_file():
                bail(404, "成片已丢失")
            # Admin replay still works during pause, but cannot exceed the
            # same global unfinished-song capacity.
            check_room_capacity()
            # Virtual replay: retain only the relationship. Source and video
            # paths are resolved on demand, including repeated replays.
            try:
                if guest_id is None:
                    store.db.execute(
                        "UPDATE songs SET priority_next=0 WHERE priority_next=1"
                    )
                cur = store.db.execute(
                    """INSERT INTO songs(
                        bvid,page,title,uploader,cover,duration,requester,requested_at,
                        status,lyrics,lyric_choice,lyric_source,prep_state,
                        skip_subtitles,priority_next,replay_of,source_type,source_id,
                        lyric_timing_mode,provider_lrc,source_lrc_raw,lyric_offset_ms
                    ) VALUES(?,?,?,?,?,?,?,?,'ready',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["bvid"],
                        row["page"],
                        row["title"],
                        row["uploader"],
                        row["cover"],
                        row["duration"],
                        guest_id if guest_id is not None else row["requester"],
                        time_now(),
                        row["lyrics"],
                        row["lyric_choice"],
                        row["lyric_source"],
                        row["prep_state"],
                        int(
                            bool(
                                row["skip_subtitles"] or row["prep_state"] == "original"
                            )
                        ),
                        int(guest_id is None),
                        song_id,
                        row["source_type"],
                        row["source_id"],
                        row["lyric_timing_mode"],
                        row["provider_lrc"],
                        row["source_lrc_raw"],
                        row["lyric_offset_ms"],
                    ),
                )
                new_id = cur.lastrowid
                store.db.commit()
            except BaseException:
                store.db.rollback()
                raise
        return {"id": new_id, "status": "ready", "priority_next": guest_id is None}

    @app.post("/api/songs/{song_id}/replay")
    def replay_own_turn(song_id: int, vc_guest: str | None = Cookie(default=None)):
        user = require_user(vc_guest)
        return schedule_replay(song_id, guest_id=user["id"])

    @app.post("/api/admin/songs/{song_id}/replay")
    def replay_song(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        return schedule_replay(song_id)

    @app.post("/api/admin/songs/{song_id}/replay-next")
    def replay_current(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "电视模式请使用主屏遥控器")
            return schedule_replay(song_id, current_only=True)

    @app.post("/api/admin/next")
    def next_song(vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "当前由电视主屏播放")
            return next_impl()

    @app.post("/api/admin/finish")
    def finish_song(body: Finish, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() == "tv":
                bail(409, "当前由电视主屏播放")
            return finish_impl(body)

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

    @app.post("/api/songs/{song_id}/cancel")
    def cancel_own_song(song_id: int, vc_guest: str | None = Cookie(default=None)):
        user = require_user(vc_guest)
        with store.lock:
            row = store.db.execute(
                "SELECT requester,status FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            if not row:
                bail(404, "歌曲不存在")
            if row["requester"] != user["id"]:
                bail(403, "只能取消自己点的歌曲")
            try:
                cur = store.db.execute(
                    "UPDATE songs SET status='cancelled' WHERE id=? "
                    "AND status IN ('waiting','ready','rendering')",
                    (song_id,),
                )
                if cur.rowcount == 0:
                    store.db.rollback()
                    bail(409, "歌曲已经开始播放，无法取消")
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        stop_song_processing(song_id)
        return {"ok": True}

    @app.post("/api/admin/songs/{song_id}/cancel")
    def cancel(song_id: int, vc_admin: str | None = Cookie(default=None)):
        require_admin(vc_admin)
        with store.lock:
            try:
                cur = store.db.execute(
                    "UPDATE songs SET status='cancelled' WHERE id=? AND status IN ('waiting','ready','rendering')",
                    (song_id,),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
            if cur.rowcount == 0:
                bail(409, "只能取消尚未播放的歌曲")
        stop_song_processing(song_id)
        return {"ok": True}

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
