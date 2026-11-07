"""Single-primary TV ownership, pairing, remote control and playback."""

import hashlib
import hmac
import json
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Cookie, HTTPException

from ..application.schemas import Finish, TVPair, TVRemoteCommand, TVRemoteReport
from ..core.store import time_now


def create_tv_router(
    *,
    store,
    events,
    require_admin,
    read_signed,
    sign,
    state_with_active,
    schedule_replay,
    signed_media,
    overlay_view,
    next_impl,
    finish_impl,
):
    router = APIRouter()

    def bail(status: int, message: str):
        raise HTTPException(status_code=status, detail=message)

    def tv_record(name: str) -> dict | None:
        row = store.db.execute(
            "SELECT value FROM room_state WHERE name=?", (name,)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def tv_save(name: str, value: dict | None):
        if value is None:
            store.db.execute("DELETE FROM room_state WHERE name=?", (name,))
        else:
            store.db.execute(
                "INSERT INTO room_state(name,value) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, json.dumps(value)),
            )

    def tv_remote_record() -> dict:
        return tv_record("tv_remote") or {
            "seq": 0,
            "ack": 0,
            "command": None,
            "issued": 0,
            "seen": 0,
            "report": {},
        }

    def tv_owner_valid(record: dict) -> bool:
        if record["role"] == "global":
            return True
        row = store.db.execute(
            "SELECT revision FROM event_admins WHERE id=?", (record["owner_id"],)
        ).fetchone()
        return bool(row and row["revision"] == record["owner_revision"])

    def tv_can_manage(principal: dict, record: dict | None) -> bool:
        return (
            record is None or principal["role"] == "global" or record["role"] == "event"
        )

    def require_tv(token: str):
        # A TV URL only contains a one-time pairing ticket. Control credentials
        # are created when the page connects, and are never in shareable links.
        identity = read_signed(token, "tv-control")
        if not identity or not events.active_id:
            bail(401, "主屏权限已失效，请管理员重新连接")
        event_id, separator, nonce = identity.partition(":")
        if not separator or event_id != events.active_id:
            bail(401, "主屏权限已失效，请管理员重新连接")
        with store.lock:
            active = tv_record("tv_primary")
            if not active:
                bail(401, "主屏已被撤回，请管理员重新连接")
            if not hmac.compare_digest(
                active["nonce_hash"], hashlib.sha256(nonce.encode()).hexdigest()
            ) or not tv_owner_valid(active):
                bail(401, "此页面不再是主屏，可能已由另一设备接管")

    @router.get("/api/admin/tv")
    def tv_management(vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        if not events.active_id:
            return {"active": False, "owner_role": None, "can_manage": False}
        with store.lock:
            active = tv_record("tv_primary")
            return {
                "active": bool(active),
                "owner_role": active["role"] if active else None,
                "can_manage": tv_can_manage(principal, active),
            }

    @router.get("/api/admin/tv/remote")
    def tv_remote_state(vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        with store.lock:
            primary = tv_record("tv_primary")
            remote = tv_remote_record()
            return {
                "online": bool(primary and remote["seen"] > time_now() - 15),
                "can_manage": tv_can_manage(principal, primary),
                "report": remote["report"],
                "pending": bool(
                    remote["seq"] > remote["ack"] and remote["issued"] > time_now() - 20
                ),
            }

    @router.post("/api/admin/tv/remote")
    def tv_remote_send(
        body: TVRemoteCommand, vc_admin: str | None = Cookie(default=None)
    ):
        principal = require_admin(vc_admin)
        with store.lock:
            if store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            primary = tv_record("tv_primary")
            if not primary or not tv_owner_valid(primary):
                bail(409, "主屏未连接")
            if not tv_can_manage(principal, primary):
                bail(403, "无权遥控全局管理员的主屏")
            remote = tv_remote_record()
            if remote["seen"] < time_now() - 15:
                bail(409, "主屏离线")
            if remote["seq"] > remote["ack"] and remote["issued"] > time_now() - 20:
                bail(409, "上一条遥控命令尚未完成")
            song_actions = ("pause", "resume", "restart", "skip", "replay_next")
            if body.action in song_actions:
                current = store.current()
                if not current or body.song_id != current["id"]:
                    bail(409, "歌曲已切换，请刷新")
            else:
                limit = {"volume": (0, 100), "offset": (-600, 600), "opacity": (0, 100)}
                if body.action == "smooth":
                    if not isinstance(body.value, bool):
                        bail(400, "无效的稳播设置")
                elif (
                    not isinstance(body.value, (int, float))
                    or isinstance(body.value, bool)
                    or not limit[body.action][0] <= body.value <= limit[body.action][1]
                ):
                    bail(400, "遥控参数超出范围")
            seq = remote["seq"] + 1
            remote.update(
                seq=seq,
                issued=time_now(),
                command={
                    "seq": seq,
                    "action": body.action,
                    "song_id": body.song_id,
                    "value": body.value,
                },
            )
            tv_save("tv_remote", remote)
            store.db.commit()
            return {"seq": seq}

    @router.post("/api/tv/report")
    def tv_report(body: TVRemoteReport, token: str):
        require_tv(token)
        with store.lock:
            remote = tv_remote_record()
            if remote["ack"] <= body.ack_seq <= remote["seq"]:
                remote["ack"] = body.ack_seq
            remote["seen"] = time_now()
            remote["report"] = body.model_dump()
            tv_save("tv_remote", remote)
            store.db.commit()
            return {"ok": True}

    @router.get("/api/admin/tv-link")
    def tv_link(
        replace: bool = False,
        vc_admin: str | None = Cookie(default=None),
    ):
        principal = require_admin(vc_admin)
        events.active()
        with store.lock:
            active = tv_record("tv_primary")
            pending = tv_record("tv_pending")
            if not tv_can_manage(principal, active):
                bail(403, "本场管理员不能撤回全局管理员的主屏")
            if (
                pending
                and pending["expires"] > time_now()
                and not tv_can_manage(principal, pending)
            ):
                bail(403, "本场管理员不能覆盖全局管理员的配对链接")
            if active and not replace:
                bail(409, "已有主屏，请使用更换主屏")
            # Reusing a pending link must not silently invalidate earlier copies.
            if (
                pending
                and pending["expires"] > time_now()
                and pending["role"] == principal["role"]
                and pending["owner_id"] == principal.get("id", "")
                and pending["owner_revision"] == principal.get("revision", 0)
                and pending["replace"] == replace
                and pending.get("pair_token")
            ):
                return {"url": "/tv.html?pair=" + quote(pending["pair_token"])}
            nonce = secrets.token_urlsafe(24)
            pair = {
                "nonce_hash": hashlib.sha256(nonce.encode()).hexdigest(),
                "role": principal["role"],
                "owner_id": principal.get("id", ""),
                "owner_revision": principal.get("revision", 0),
                "replace": replace,
                "expires": time_now() + 600,
                "pair_token": sign("tv-pair", f"{events.active_id}:{nonce}", 600),
            }
            tv_save("tv_pending", pair)
            store.db.commit()
            return {"url": "/tv.html?pair=" + quote(pair["pair_token"])}

    @router.post("/api/tv/activate")
    def tv_activate(body: TVPair):
        identity = read_signed(body.pair, "tv-pair")
        if not identity or not events.active_id:
            bail(401, "主屏配对链接已过期，请管理员重新生成")
        event_id, sep, nonce = identity.partition(":")
        if not sep or event_id != events.active_id:
            bail(401, "主屏配对链接已过期，请管理员重新生成")
        with store.lock:
            pending = tv_record("tv_pending")
            active = tv_record("tv_primary")
            if (
                not pending
                or pending["expires"] < time_now()
                or not hmac.compare_digest(
                    pending["nonce_hash"], hashlib.sha256(nonce.encode()).hexdigest()
                )
                or not tv_owner_valid(pending)
            ):
                bail(409, "主屏配对链接已使用或失效，请管理员重新生成")
            if active and (
                not pending["replace"]
                or (active["role"] == "global" and pending["role"] != "global")
            ):
                bail(409, "主屏已被其他管理员占用，请重新连接")
            control_nonce = secrets.token_urlsafe(32)
            tv_save(
                "tv_primary",
                {
                    "nonce_hash": hashlib.sha256(control_nonce.encode()).hexdigest(),
                    "role": pending["role"],
                    "owner_id": pending["owner_id"],
                    "owner_revision": pending["owner_revision"],
                },
            )
            tv_save("tv_pending", None)
            tv_save("tv_remote", None)
            store.db.commit()
            return {
                "token": sign(
                    "tv-control", f"{events.active_id}:{control_nonce}", 8 * 3600
                )
            }

    @router.post("/api/admin/tv/revoke")
    def tv_revoke(vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        with store.lock:
            active = tv_record("tv_primary")
            pending = tv_record("tv_pending")
            if not tv_can_manage(principal, active):
                bail(403, "本场管理员不能撤回全局管理员的主屏")
            if not active and not tv_can_manage(principal, pending):
                bail(403, "本场管理员不能撤回全局管理员的配对链接")
            tv_save("tv_primary", None)
            tv_save("tv_remote", None)
            if tv_can_manage(principal, pending):
                tv_save("tv_pending", None)
            store.db.commit()
        return {"ok": True}

    @router.get("/api/tv/state")
    def tv_state(token: str):
        require_tv(token)
        with store.lock:
            snapshot = state_with_active()
            remote = tv_remote_record()
            snapshot["remote_command"] = (
                remote["command"]
                if store.playback_mode() == "tv" and remote["seq"] > remote["ack"]
                else None
            )
            return snapshot

    @router.post("/api/tv/replay-next")
    def tv_replay_next(body: Finish, token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            return schedule_replay(body.song_id, current_only=True)

    @router.get("/api/tv/preload")
    def tv_preload(token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() != "tv" or not store.current():
                return {"candidate": None}
            # A preview, never a claim: do not advance fair counts or song state.
            queue = store.fair_queue()
            if not queue:
                return {"candidate": None}
            song = queue[0]
            media = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (song["id"],)
            ).fetchone()
            assets = store.replay_assets(media)
            return {
                "candidate": {
                    "id": song["id"],
                    "media_file": assets["media_file"],
                    "source_file": assets["source_file"],
                    "media_url": signed_media(song),
                    "html_overlay": overlay_view(dict(media)),
                }
            }

    @router.post("/api/tv/next")
    def tv_next(token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            return next_impl()

    @router.post("/api/tv/finish")
    def tv_finish(body: Finish, token: str):
        require_tv(token)
        with store.lock:
            if store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            return finish_impl(body)

    return router
