"""Small on-disk KTV event catalog: one active Store and directory per event."""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import sqlite3
import threading
from contextlib import contextmanager
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
        self._archive_busy: set[str] = set()
        self.idle = threading.Condition(self.lock)
        self.inflight = 0
        self.transitioning = False
        self.store_factory = store_factory
        self.active_id: str | None = None
        self.current_store = None
        self.pointer = root / "active-session"
        self.creating_marker = root / "creating-session"
        self.closing_marker = root / "closing-session"
        self._recover_transitions()

        if self.pointer.exists():
            event_id = self.pointer.read_text().strip()
            if not EVENT_ID.fullmatch(event_id):
                raise RuntimeError("Invalid active event pointer")
            folder = self.base / event_id
            if not (folder / "info.json").is_file():
                raise RuntimeError("Active event directory is missing")
            info = json.loads((folder / "info.json").read_text())
            if info.get("status") != "active":
                raise RuntimeError("Active event metadata is not active")
            self.active_id = event_id
            self.current_store = store_factory(folder)
        elif (root / "room.sqlite3").exists() and not (root / "legacy.closed").exists():
            # Existing installation: keep old data intact until offline migration.
            self.active_id = "legacy"
            self.current_store = store_factory(root)

    def _quarantine_pointer(self) -> None:
        path = self.root / f".invalid-active-session-{secrets.token_hex(4)}"
        self.pointer.replace(path)
        logging.getLogger(__name__).error(
            "Invalid active-session pointer moved to %s; retained event data", path
        )

    def _recover_transitions(self) -> None:
        """Resolve crashes between independent on-disk event transition steps."""
        logger = logging.getLogger(__name__)
        if self.creating_marker.is_file():
            event_id = self.creating_marker.read_text().strip()
            if not EVENT_ID.fullmatch(event_id) or event_id == "legacy":
                raise RuntimeError("Invalid pending event creation marker")
            is_published = (
                self.pointer.is_file() and self.pointer.read_text().strip() == event_id
            )
            if not is_published:
                folder = self.base / event_id
                has_songs = False
                dbfile = folder / "room.sqlite3"
                if dbfile.is_file():
                    try:
                        with sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True) as db:
                            has_songs = bool(
                                db.execute("SELECT 1 FROM songs LIMIT 1").fetchone()
                            )
                    except sqlite3.Error:
                        has_songs = (
                            True  # Preserve incomplete or damaged data for inspection.
                        )
                if has_songs:
                    logger.error(
                        "Unpublished event %s has song data; retaining it", event_id
                    )
                elif folder.is_dir() and not folder.is_symlink():
                    shutil.rmtree(folder)
                if self.pointer.is_file() and not EVENT_ID.fullmatch(
                    self.pointer.read_text().strip()
                ):
                    self._quarantine_pointer()
            self.creating_marker.unlink()
            logger.warning(
                "Reconciled interrupted event creation: published=%s", is_published
            )

        if self.closing_marker.is_file():
            event_id = self.closing_marker.read_text().strip()
            if not EVENT_ID.fullmatch(event_id) or event_id == "legacy":
                raise RuntimeError("Invalid pending event close marker")
            folder = self.base / event_id
            info_path = folder / "info.json"
            if not info_path.is_file() or not (folder / "room.sqlite3").is_file():
                raise RuntimeError(
                    "Cannot recover incomplete close: event data missing"
                )
            info = json.loads(info_path.read_text())
            if info.get("id") != event_id:
                raise RuntimeError("Cannot recover incomplete close: event id mismatch")
            if info.get("status") != "closed":
                store = self.store_factory(folder)
                try:
                    with store.lock:
                        if store.db.execute(
                            "SELECT 1 FROM songs WHERE archive_order IS NULL LIMIT 1"
                        ).fetchone():
                            order = store.close_song_order()
                            store.db.executemany(
                                "UPDATE songs SET archive_order=? WHERE id=?",
                                [(i, song_id) for i, song_id in enumerate(order, 1)],
                            )
                        store.db.execute(
                            "UPDATE songs SET status='closed_interrupted' WHERE status='playing'"
                        )
                        store.db.execute(
                            "UPDATE songs SET status='closed_unplayed' "
                            "WHERE status IN ('waiting','rendering','ready')"
                        )
                        store.db.commit()
                finally:
                    store.db.close()
                self._write_atomic(
                    info_path,
                    json.dumps(
                        {
                            **info,
                            "status": "closed",
                            "closed_at": datetime.now().isoformat(timespec="seconds"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
            if self.pointer.is_file() and self.pointer.read_text().strip() == event_id:
                self.pointer.unlink()
            self.closing_marker.unlink()
            logger.warning("Recovered interrupted close of event %s", event_id)

        if self.pointer.is_file():
            value = self.pointer.read_text().strip()
            if not EVENT_ID.fullmatch(value):
                self._quarantine_pointer()
            elif value != "legacy":
                folder = self.base / value
                info_path = folder / "info.json"
                try:
                    info = json.loads(info_path.read_text())
                    if info.get("id") != value:
                        raise ValueError("Event metadata ID mismatch")
                except (OSError, ValueError):
                    self._quarantine_pointer()
                else:
                    if info.get("status") == "closed":
                        self.pointer.unlink()
                    elif (
                        info.get("status") != "active"
                        or not (folder / "room.sqlite3").is_file()
                    ):
                        self._quarantine_pointer()

        if not self.pointer.exists():
            # Reclaim one complete orphan from older code, which wrote active
            # metadata before publishing the pointer. Never pick ambiguously.
            orphans = []
            for folder in self.base.iterdir():
                if (
                    not folder.is_dir()
                    or folder.is_symlink()
                    or not EVENT_ID.fullmatch(folder.name)
                ):
                    continue
                if (
                    not (folder / "info.json").is_file()
                    or not (folder / "room.sqlite3").is_file()
                ):
                    continue
                try:
                    info = json.loads((folder / "info.json").read_text())
                except ValueError:
                    continue
                if info.get("id") == folder.name and info.get("status") == "active":
                    orphans.append(folder.name)
            if len(orphans) == 1:
                self._write_atomic(self.pointer, orphans[0] + "\n")
                logger.warning("Reattached an orphan active event: %s", orphans[0])
            elif len(orphans) > 1:
                logger.error("Multiple orphan active events; manual recovery needed")

    def mark_closing(self) -> None:
        if self.active_id and self.active_id != "legacy":
            self._write_atomic(self.closing_marker, self.active_id + "\n")

    def clear_closing(self) -> None:
        self.closing_marker.unlink(missing_ok=True)

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
                try:
                    items.append(json.loads((f / "info.json").read_text()))
                except FileNotFoundError:
                    # A closed room may be deleted between is_file and read.
                    continue
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

    def closed_folder(self, event_id: str):
        """Only normal closed sessions may have their files modified."""
        if not EVENT_ID.fullmatch(event_id):
            raise HTTPException(404, "场次不存在")
        if event_id == "legacy":
            raise HTTPException(409, "旧版场次不支持此操作")
        if event_id == self.active_id:
            raise HTTPException(409, "只能操作已结束的场次")
        folder = self.base / event_id
        info = folder / "info.json"
        if (
            folder.is_symlink()
            or not folder.is_dir()
            or info.is_symlink()
            or not info.is_file()
        ):
            raise HTTPException(404, "场次不存在")
        entry = json.loads(info.read_text())
        if entry.get("id") != event_id or entry.get("status") != "closed":
            raise HTTPException(409, "只能操作已结束的场次")
        return folder, entry

    @contextmanager
    def archive_operation(self, event_id: str):
        with self.lock:
            folder, entry = self.closed_folder(event_id)
            if event_id in self._archive_busy:
                raise HTTPException(409, "该场次正在清理中")
            self._archive_busy.add(event_id)
        try:
            yield folder, entry
        finally:
            with self.lock:
                self._archive_busy.remove(event_id)

    def release_media(self, event_id: str, csv_text: str):
        """Retain the event DB, CSV and small text; remove bulky media."""
        with self.archive_operation(event_id) as (folder, entry):
            self._write_atomic(
                folder / "songs.csv", csv_text() if callable(csv_text) else csv_text
            )
            media = folder / "media"
            freed = 0
            keep_text = {
                ".csv",
                ".json",
                ".txt",
                ".lrc",
                ".ass",
                ".srt",
                ".vtt",
                ".md",
                ".textgrid",
            }
            if media.is_symlink():
                media.unlink()
            elif media.is_dir():
                for file in sorted(
                    media.rglob("*"), key=lambda p: len(p.parts), reverse=True
                ):
                    if file.is_symlink():
                        file.unlink()
                    elif file.is_dir():
                        if not any(file.iterdir()):
                            file.rmdir()
                    elif file.is_file():
                        size = file.stat().st_size
                        if file.suffix.lower() in keep_text and size <= 1024 * 1024:
                            continue
                        file.unlink()
                        freed += size
                if not any(media.iterdir()):
                    media.rmdir()
            entry["media_released_at"] = entry.get(
                "media_released_at"
            ) or datetime.now().isoformat(timespec="seconds")
            self._write_atomic(
                folder / "info.json",
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n",
            )
            return {
                "freed_bytes": freed,
                "media_released_at": entry["media_released_at"],
            }

    def delete_closed(self, event_id: str):
        with self.archive_operation(event_id) as (folder, _):
            shutil.rmtree(folder)
            return {"ok": True}

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
                self._write_atomic(self.creating_marker, event_id + "\n")
                entry = {
                    "id": event_id,
                    "name": name.strip(),
                    "code": code or f"{secrets.randbelow(1000000):06d}",
                    "status": "active",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                new_store = None
                try:
                    folder.mkdir(mode=0o700)
                    (folder / "info.json").write_text(
                        json.dumps(entry, ensure_ascii=False, indent=2) + "\n"
                    )
                    (folder / "info.json").chmod(0o600)
                    new_store = self.store_factory(folder)
                    # Only new rooms receive new defaults; existing rooms are unchanged.
                    new_store.save_settings(
                        None, None, video_quality=1080, original_codec_mode="av1"
                    )
                    self._write_atomic(self.pointer, event_id + "\n")
                    self.creating_marker.unlink(missing_ok=True)
                except BaseException:
                    # The event is not published. A partially written pointer
                    # would prevent startup, and info.json alone looks active.
                    if new_store is not None:
                        new_store.db.close()
                    self.pointer.unlink(missing_ok=True)
                    if folder.is_dir():
                        shutil.rmtree(folder)
                    self.creating_marker.unlink(missing_ok=True)
                    raise
                self.current_store = new_store
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

    @staticmethod
    def _write_atomic(path: Path, content: str):
        temporary = path.with_name(f".{path.name}-{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(content)
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _finish_close(self):
        current = self.metadata()
        assert self.active_id is not None and current is not None
        # Commit before changing any on-disk event state, so a failed DB
        # commit cannot leave an active session marked as closed.
        if self.current_store:
            self.current_store.db.commit()
        if self.active_id == "legacy":
            self._write_atomic(self.root / "legacy.closed", "closed\n")
        else:
            info = self.base / self.active_id / "info.json"
            closed = {**current, "status": "closed"}
            closed["closed_at"] = datetime.now().isoformat(timespec="seconds")
            self._write_atomic(
                info, json.dumps(closed, ensure_ascii=False, indent=2) + "\n"
            )
            try:
                self.pointer.unlink(missing_ok=True)
            except BaseException:
                # The pointer still names this event; restore its active
                # metadata and leave the Store open so closing can be retried.
                self._write_atomic(
                    info, json.dumps(current, ensure_ascii=False, indent=2) + "\n"
                )
                raise
            current = closed
        if self.current_store:
            self.current_store.db.close()
        self.current_store = None
        self.active_id = None
        self.clear_closing()
        return current


class CurrentStore:
    """Forward existing Store operations to the currently active event."""

    def __init__(self, events: Events):
        self.events = events

    @property
    def lock(self):
        """Guard both the event pointer and its worker-shared SQLite connection."""
        return self._locked()

    @contextmanager
    def _locked(self):
        # Pin the selected event for the entire Store transaction. Closing a
        # session waits for these pins, so Events.lock need not be held while
        # waiting for or using the worker-shared SQLite mutex.
        with self.events.lock:
            current = self.events.current_store
            if current is None:
                yield
                return
            self.events.inflight += 1
        try:
            with current.lock:
                yield
        finally:
            self.events.end_request()

    def __getattr__(self, name):
        return getattr(self.events.active(), name)
