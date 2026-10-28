"""Small on-disk KTV event catalog: one active Store and directory per event."""

from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

EVENT_ID = re.compile(r"^[a-zA-Z0-9_-]{1,60}$")


class Events:
    def __init__(self, root: Path, store_factory):
        self.root = root
        self.base = root / "sessions"
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.base.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.idle = threading.Condition(self.lock)
        self.inflight = 0
        self.transitioning = False
        self.store_factory = store_factory
        self.active_id: str | None = None
        self.current_store = None
        self.pointer = root / "active-session"

        if self.pointer.exists():
            event_id = self.pointer.read_text().strip()
            if not EVENT_ID.fullmatch(event_id):
                raise RuntimeError("Invalid active event pointer")
            folder = self.base / event_id
            if not (folder / "info.json").is_file():
                raise RuntimeError("Active event directory is missing")
            self.active_id = event_id
            self.current_store = store_factory(folder)
        elif (root / "room.sqlite3").exists() and not (root / "legacy.closed").exists():
            # Existing installation: keep old data intact until offline migration.
            self.active_id = "legacy"
            self.current_store = store_factory(root)

    def begin_request(self) -> bool:
        # A request uses the same event throughout its lifetime. Do not keep
        # the lock held while it performs IO or awaits external lyric services.
        with self.lock:
            if self.transitioning:
                return False
            self.inflight += 1
            return True

    def end_request(self):
        with self.lock:
            self.inflight -= 1
            self.idle.notify_all()

    def active(self):
        if not self.current_store:
            raise HTTPException(409, "尚未创建 KTV 场次")
        return self.current_store

    def metadata(self):
        if not self.active_id:
            return None
        if (
            self.active_id == "legacy"
            and not (self.base / "legacy" / "info.json").exists()
        ):
            return {
                "id": "legacy",
                "name": "原有 KTV",
                "status": "active",
                "legacy": True,
            }
        return json.loads((self.base / self.active_id / "info.json").read_text())

    def list(self):
        items = []
        if (self.root / "room.sqlite3").exists():
            items.append(
                {
                    "id": "legacy",
                    "name": "原有 KTV",
                    "status": "active" if self.active_id == "legacy" else "closed",
                    "legacy": True,
                }
            )
        for f in sorted(self.base.iterdir(), reverse=True):
            if (
                f.is_dir()
                and (f / "info.json").is_file()
                and EVENT_ID.fullmatch(f.name)
            ):
                items.append(json.loads((f / "info.json").read_text()))
        return items

    def database_path(self, event_id: str) -> Path:
        """Existing per-event SQLite file; never create missing archives."""
        if not EVENT_ID.fullmatch(event_id):
            raise HTTPException(404, "场次不存在")
        if event_id == "legacy":
            folder = self.base / "legacy"
            path = (
                folder / "room.sqlite3"
                if folder.is_dir()
                else self.root / "room.sqlite3"
            )
        else:
            folder = self.base / event_id
            path = folder / "room.sqlite3"
        if not path.is_file() or event_id not in {e["id"] for e in self.list()}:
            raise HTTPException(404, "场次不存在")
        return path

    def create(self, name: str, code: str | None = None):
        with self.lock:
            if self.active_id:
                raise HTTPException(409, "请先结束当前场次")
            if not 1 <= len(name.strip()) <= 60:
                raise HTTPException(400, "场次名称须为 1-60 字")
            if code is not None and (
                len(code) != 6 or not code.isascii() or not code.isdecimal()
            ):
                raise HTTPException(400, "加入密码必须是六位数字")
            self.transitioning = True
            try:
                # Even a request admitted with no active event must not
                # unexpectedly start using the newly created Store.
                if not self.idle.wait_for(lambda: self.inflight == 0, timeout=10):
                    raise HTTPException(409, "仍有请求正在处理，请稍后重试")
                event_id = (
                    datetime.now().strftime("%Y%m%d-%H%M%S")
                    + "-"
                    + secrets.token_hex(4)
                )
                folder = self.base / event_id
                folder.mkdir(mode=0o700)
                entry = {
                    "id": event_id,
                    "name": name.strip(),
                    "code": code or f"{secrets.randbelow(1000000):06d}",
                    "status": "active",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                (folder / "info.json").write_text(
                    json.dumps(entry, ensure_ascii=False, indent=2) + "\n"
                )
                (folder / "info.json").chmod(0o600)
                self.current_store = self.store_factory(folder)
                self.pointer.write_text(event_id + "\n")
                self.active_id = event_id
                return entry
            finally:
                self.transitioning = False

    def close(self):
        with self.lock:
            if not self.active_id:
                raise HTTPException(409, "没有活动场次")
            self.transitioning = True
            try:
                # New requests receive 409 while the last old-event reader
                # finishes. Condition.wait releases the RLock while waiting.
                if not self.idle.wait_for(lambda: self.inflight == 0, timeout=10):
                    raise HTTPException(409, "仍有请求正在处理，请稍后重试")
                return self._finish_close()
            finally:
                self.transitioning = False

    def _finish_close(self):
        current = self.metadata()
        if self.active_id == "legacy":
            (self.root / "legacy.closed").write_text("closed\n")
        else:
            current["status"] = "closed"
            current["closed_at"] = datetime.now().isoformat(timespec="seconds")
            (self.base / self.active_id / "info.json").write_text(
                json.dumps(current, ensure_ascii=False, indent=2) + "\n"
            )
        if self.current_store:
            self.current_store.db.commit()
            self.current_store.db.close()
        self.current_store = None
        self.active_id = None
        self.pointer.unlink(missing_ok=True)
        return current


class CurrentStore:
    """Forward existing Store operations to the currently active event."""

    def __init__(self, events: Events):
        self.events = events
        self.lock = events.lock

    def __getattr__(self, name):
        return getattr(self.events.active(), name)
