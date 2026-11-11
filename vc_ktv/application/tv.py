"""Single-primary TV authority, pairing and remote-command state transitions."""

import hashlib
import hmac
import json
import secrets
from urllib.parse import quote

from vc_ktv.application.errors import bail
from vc_ktv.application.tv_notifications import TVNotifications
from vc_ktv.core.store import time_now


class TVSessionService:
    def __init__(self, store, events, auth):
        self.store = store
        self.events = events
        self.auth = auth
        self.notifications = TVNotifications()

    def read_record(self, name: str) -> dict | None:
        row = self.store.db.execute(
            "SELECT value FROM room_state WHERE name=?", (name,)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def write_record(self, name: str, value: dict | None) -> None:
        if value is None:
            self.store.db.execute("DELETE FROM room_state WHERE name=?", (name,))
        else:
            self.store.db.execute(
                "INSERT INTO room_state(name,value) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, json.dumps(value)),
            )

    def remote_record(self) -> dict:
        return self.read_record("tv_remote") or {
            "seq": 0,
            "ack": 0,
            "command": None,
            "issued": 0,
            "seen": 0,
            "report": {},
        }

    def owner_valid(self, record: dict) -> bool:
        if record["role"] == "global":
            return True
        row = self.store.db.execute(
            "SELECT revision FROM event_admins WHERE id=?", (record["owner_id"],)
        ).fetchone()
        return bool(row and row["revision"] == record["owner_revision"])

    @staticmethod
    def can_manage(principal: dict, record: dict | None) -> bool:
        return (
            record is None or principal["role"] == "global" or record["role"] == "event"
        )

    def require_tv(self, token: str) -> None:
        identity = self.auth.read_signed(token, "tv-control")
        if not identity or not self.events.active_id:
            bail(401, "主屏权限已失效，请管理员重新连接")
        event_id, separator, nonce = identity.partition(":")
        if not separator or event_id != self.events.active_id:
            bail(401, "主屏权限已失效，请管理员重新连接")
        with self.store.lock:
            active = self.read_record("tv_primary")
            if not active:
                bail(401, "主屏已被撤回，请管理员重新连接")
            if not hmac.compare_digest(
                active["nonce_hash"], hashlib.sha256(nonce.encode()).hexdigest()
            ) or not self.owner_valid(active):
                bail(401, "此页面不再是主屏，可能已由另一设备接管")

    def management(self, principal: dict) -> dict:
        if not self.events.active_id:
            return {"active": False, "owner_role": None, "can_manage": False}
        with self.store.lock:
            active = self.read_record("tv_primary")
            return {
                "active": bool(active),
                "owner_role": active["role"] if active else None,
                "can_manage": self.can_manage(principal, active),
            }

    def remote_state(self, principal: dict) -> dict:
        with self.store.lock:
            primary = self.read_record("tv_primary")
            remote = self.remote_record()
            return {
                "online": bool(primary and remote["seen"] > time_now() - 15),
                "can_manage": self.can_manage(principal, primary),
                "report": remote["report"],
                "pending": bool(
                    remote["seq"] > remote["ack"] and remote["issued"] > time_now() - 20
                ),
            }

    def send_command(
        self, principal: dict, action: str, song_id: int | None, value
    ) -> dict:
        with self.store.lock:
            if self.store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            primary = self.read_record("tv_primary")
            if not primary or not self.owner_valid(primary):
                bail(409, "主屏未连接")
            if not self.can_manage(principal, primary):
                bail(403, "无权遥控全局管理员的主屏")
            remote = self.remote_record()
            if remote["seen"] < time_now() - 15:
                bail(409, "主屏离线")
            if remote["seq"] > remote["ack"] and remote["issued"] > time_now() - 20:
                bail(409, "上一条遥控命令尚未完成")
            song_actions = ("pause", "resume", "restart", "skip", "replay_next")
            if action in song_actions:
                current = self.store.current()
                if not current or song_id != current["id"]:
                    bail(409, "歌曲已切换，请刷新")
            else:
                limit = {"volume": (0, 100), "offset": (-600, 600), "opacity": (0, 100)}
                if action == "smooth":
                    if not isinstance(value, bool):
                        bail(400, "无效的稳播设置")
                elif (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not limit[action][0] <= value <= limit[action][1]
                ):
                    bail(400, "遥控参数超出范围")
            seq = remote["seq"] + 1
            remote.update(
                seq=seq,
                issued=time_now(),
                command={
                    "seq": seq,
                    "action": action,
                    "song_id": song_id,
                    "value": value,
                },
            )
            self.write_record("tv_remote", remote)
            self.store.db.commit()
            self.notifications.notify("tv")
            return {"seq": seq}

    def report(self, ack_seq: int, report: dict) -> dict:
        with self.store.lock:
            remote = self.remote_record()
            if remote["ack"] <= ack_seq <= remote["seq"]:
                remote["ack"] = ack_seq
            remote["seen"] = time_now()
            remote["report"] = report
            self.write_record("tv_remote", remote)
            self.store.db.commit()
            self.notifications.notify("admin")
            return {"ok": True}

    def link(self, principal: dict, replace: bool = False) -> dict:
        self.events.active()
        with self.store.lock:
            active = self.read_record("tv_primary")
            pending = self.read_record("tv_pending")
            if not self.can_manage(principal, active):
                bail(403, "本场管理员不能撤回全局管理员的主屏")
            if (
                pending
                and pending["expires"] > time_now()
                and not self.can_manage(principal, pending)
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
                "pair_token": self.auth.sign(
                    "tv-pair", f"{self.events.active_id}:{nonce}", 600
                ),
            }
            self.write_record("tv_pending", pair)
            self.store.db.commit()
            return {"url": "/tv.html?pair=" + quote(pair["pair_token"])}

    def activate(self, pair: str) -> dict:
        identity = self.auth.read_signed(pair, "tv-pair")
        if not identity or not self.events.active_id:
            bail(401, "主屏配对链接已过期，请管理员重新生成")
        event_id, sep, nonce = identity.partition(":")
        if not sep or event_id != self.events.active_id:
            bail(401, "主屏配对链接已过期，请管理员重新生成")
        with self.store.lock:
            pending = self.read_record("tv_pending")
            active = self.read_record("tv_primary")
            if (
                not pending
                or pending["expires"] < time_now()
                or not hmac.compare_digest(
                    pending["nonce_hash"], hashlib.sha256(nonce.encode()).hexdigest()
                )
                or not self.owner_valid(pending)
            ):
                bail(409, "主屏配对链接已使用或失效，请管理员重新生成")
            if active and (
                not pending["replace"]
                or (active["role"] == "global" and pending["role"] != "global")
            ):
                bail(409, "主屏已被其他管理员占用，请重新连接")
            control_nonce = secrets.token_urlsafe(32)
            self.write_record(
                "tv_primary",
                {
                    "nonce_hash": hashlib.sha256(control_nonce.encode()).hexdigest(),
                    "role": pending["role"],
                    "owner_id": pending["owner_id"],
                    "owner_revision": pending["owner_revision"],
                },
            )
            self.write_record("tv_pending", None)
            self.write_record("tv_remote", None)
            self.store.db.commit()
            self.notifications.notify("tv")
            self.notifications.notify("admin")
            return {
                "token": self.auth.sign(
                    "tv-control", f"{self.events.active_id}:{control_nonce}", 8 * 3600
                )
            }

    def _return_current_to_queue(self) -> int | None:
        """Undo an uncompleted TV claim without charging a singing turn.

        Called under Store.lock and committed with the surrounding TV change.
        Existing admin-selected next-song priority is preserved.
        """
        current = self.store.current()
        if not current:
            return None
        self.store.db.execute(
            "UPDATE songs SET status='ready',played_at=NULL,fair_exempt=0 "
            "WHERE id=? AND status='playing'",
            (current["id"],),
        )
        return current["id"]

    def restore_current(self, principal: dict) -> dict:
        """Recover a stranded TV turn only after the controlling TV is gone."""
        with self.store.lock:
            if self.store.playback_mode() != "tv":
                bail(409, "当前不是电视播放模式")
            if self.read_record("tv_primary"):
                bail(409, "主屏仍在连接，请先撤回主屏")
            try:
                song_id = self._return_current_to_queue()
                if song_id is None:
                    bail(409, "没有需要恢复的当前歌曲")
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise
            self.notifications.notify("admin")
            return {"ok": True, "requeued_song_id": song_id}

    def revoke(self, principal: dict) -> dict:
        with self.store.lock:
            active = self.read_record("tv_primary")
            pending = self.read_record("tv_pending")
            if not self.can_manage(principal, active):
                bail(403, "本场管理员不能撤回全局管理员的主屏")
            if not active and not self.can_manage(principal, pending):
                bail(403, "本场管理员不能撤回全局管理员的配对链接")
            try:
                requeued = (
                    self._return_current_to_queue()
                    if active and self.store.playback_mode() == "tv"
                    else None
                )
                self.write_record("tv_primary", None)
                self.write_record("tv_remote", None)
                if self.can_manage(principal, pending):
                    self.write_record("tv_pending", None)
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise
            self.notifications.notify("tv")
            self.notifications.notify("admin")
        return {"ok": True, "requeued_song_id": requeued}

    def require_tv_mode(self) -> None:
        if self.store.playback_mode() != "tv":
            bail(409, "当前不是电视播放模式")

    def state(self, media) -> dict:
        with self.store.lock:
            snapshot = media.state_with_active()
            remote = self.remote_record()
            snapshot["remote_command"] = (
                remote["command"]
                if self.store.playback_mode() == "tv" and remote["seq"] > remote["ack"]
                else None
            )
            return snapshot
