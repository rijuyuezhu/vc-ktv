"""Read-only event song history and canonical CSV archive formatting."""

import csv
import io
import json
import sqlite3
from contextlib import closing, nullcontext
from typing import Literal
from urllib.parse import quote

from fastapi import Response

from vc_ktv.application.errors import bail


class EventArchiveService:
    def __init__(self, events, auth):
        self.events = events
        self.auth = auth

    def require_history_viewer(
        self, guest: str | None, admin: str | None
    ) -> dict | None:
        principal = self.auth.admin_principal(admin)
        if principal is None and self.auth.current_user(guest) is None:
            bail(401, "请先加入本场 KTV 并登录")
        return principal

    def visible_event(
        self, event_id: str, guest: str | None, admin: str | None
    ) -> dict:
        principal = self.require_history_viewer(guest, admin)
        with self.events.lock:
            event = next(
                (item for item in self.events.list() if item["id"] == event_id), None
            )
            if (
                not event
                or event.get("status") != "closed"
                or not (
                    event.get("public", False)
                    or (principal and principal["role"] == "global")
                )
            ):
                bail(404, "场次不存在")
            return {
                key: event.get(key)
                for key in (
                    "id",
                    "name",
                    "status",
                    "created_at",
                    "closed_at",
                    "media_released_at",
                    "public",
                    "legacy",
                )
            }

    def cached_cover(self, event_id: str, song_id: int):
        if song_id < 1:
            return None
        media = self.events.database_path(event_id).parent / "media"
        folder = media / "covers"
        if media.is_symlink() or folder.is_symlink():
            return None
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = folder / f"{song_id}{ext}"
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        return None

    def with_cached_cover(self, event_id: str, song: dict) -> dict:
        if self.cached_cover(event_id, song["id"]):
            song["cover"] = f"/api/events/{event_id}/covers/{song['id']}"
        return song

    def public_events(self, guest: str | None, admin: str | None) -> list[dict]:
        self.require_history_viewer(guest, admin)
        with self.events.lock:
            return [
                {
                    key: item.get(key)
                    for key in ("id", "name", "created_at", "closed_at")
                }
                for item in self.events.list()
                if item.get("status") == "closed" and item.get("public") is True
            ]

    def public_records(
        self, event_id: str, guest: str | None, admin: str | None
    ) -> dict:
        with self.events.lock:
            event = self.visible_event(event_id, guest, admin)
            return {"event": event, "songs": self._read_records(event_id)}

    def song_detail(
        self, event_id: str, song_id: int, guest: str | None, admin: str | None
    ) -> dict:
        with self.events.lock:
            event = self.visible_event(event_id, guest, admin)
            path = self.events.database_path(event_id)
            with sqlite3.connect(
                f"file:{quote(str(path), safe='/')}?mode=ro", uri=True
            ) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    """SELECT s.id,s.title,s.bvid,s.page,s.source_type,s.source_id,
                       s.uploader,s.cover,s.duration,s.status,
                       CASE WHEN s.status IN ('played','skipped')
                            THEN s.played_at END AS played_at,
                       g.name AS nickname FROM songs s JOIN guests g ON g.id=s.requester
                       WHERE s.id=?""",
                    (song_id,),
                ).fetchone()
                if row is None:
                    bail(404, "歌曲不存在")
                return {
                    "event": event,
                    "song": self.with_cached_cover(event_id, dict(row)),
                }

    def set_visibility(self, event_id: str, value: bool, token: str | None) -> dict:
        principal = self.auth.require_admin(token)
        if principal["role"] != "global":
            bail(403, "仅全局管理员可管理历史场次")
        with self.events.archive_operation(event_id) as (folder, entry):
            entry["public"] = value
            self.events._write_atomic(
                folder / "info.json",
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n",
            )
            return {"public": value}

    def update_status(
        self,
        event_id: str,
        song_id: int,
        status: Literal[
            "played", "skipped", "cancelled", "closed_unplayed", "closed_interrupted"
        ],
        token: str | None,
    ) -> dict:
        principal = self.auth.require_admin(token)
        if principal["role"] != "global":
            bail(403, "仅全局管理员可修改历史歌曲")
        with self.events.archive_operation(event_id) as (folder, _):
            with sqlite3.connect(folder / "room.sqlite3", timeout=10) as db:
                row = db.execute(
                    "SELECT status FROM songs WHERE id=?", (song_id,)
                ).fetchone()
                if not row:
                    bail(404, "歌曲不存在")
                allowed = {
                    "played",
                    "skipped",
                    "cancelled",
                    "closed_unplayed",
                    "closed_interrupted",
                }
                if row[0] not in allowed:
                    bail(409, "歌曲仍在进行中")
                # Keep any real playback timestamp for later corrections.
                # Public records only show it for played/skipped statuses.
                db.execute("UPDATE songs SET status=? WHERE id=?", (status, song_id))
                csv_path = folder / "songs.csv"
                old_csv = csv_path.read_text() if csv_path.is_file() else None
                if old_csv is not None:
                    # Generate the snapshot from this uncommitted transaction:
                    # a failed CSV write rolls back the status change too.
                    self.events._write_atomic(
                        csv_path, self.csv_text(event_id, token, connection=db)
                    )
                try:
                    db.commit()
                except Exception:
                    if old_csv is not None:
                        self.events._write_atomic(csv_path, old_csv)
                    raise
            return {"id": song_id, "status": status}

    def records(self, event_id: str, token: str | None) -> list[dict]:
        principal = self.auth.require_admin(token)
        if principal["role"] == "event" and event_id != self.events.active_id:
            bail(403, "无权查看其他场次历史")
        return self._read_records(event_id)

    def _read_records(
        self, event_id: str, *, connection: sqlite3.Connection | None = None
    ) -> list[dict]:
        with self.events.lock:
            path = self.events.database_path(event_id)
            reader = (
                nullcontext(connection)
                if connection is not None
                else closing(
                    sqlite3.connect(
                        f"file:{quote(str(path), safe='/')}?mode=ro", uri=True
                    )
                )
            )
            with reader as db:
                db.row_factory = sqlite3.Row
                columns = {
                    column[1] for column in db.execute("PRAGMA table_info(songs)")
                }
                frozen = (
                    "archive_order" in columns
                    and db.execute(
                        "SELECT 1 FROM songs WHERE archive_order IS NOT NULL LIMIT 1"
                    ).fetchone()
                )
                ordering = (
                    "ORDER BY CASE WHEN s.archive_order IS NULL THEN 1 ELSE 0 END, "
                    "s.archive_order,s.requested_at,s.id"
                    if frozen
                    else """ORDER BY
                        CASE WHEN s.status IN ('played','skipped') THEN 0
                             WHEN s.status IN ('playing','closed_interrupted') THEN 1
                             WHEN s.status='cancelled' THEN 3
                             ELSE 2 END,
                        CASE WHEN s.status IN ('played','skipped') THEN s.played_at END,
                        s.requested_at,s.id"""
                )
                songs = [
                    dict(row)
                    for row in db.execute(
                        """SELECT s.id,s.title,g.name AS requester,
                                  s.status,s.source_type,s.source_id,s.bvid,
                                  s.page,s.requested_at,
                                  CASE WHEN s.status IN ('played','skipped')
                                       THEN s.played_at END AS played_at,
                                  s.uploader
                           FROM songs s JOIN guests g ON g.id=s.requester
                           """
                        + ordering
                    )
                ]
            if event_id == self.events.active_id:
                rank = {
                    song_id: i
                    for i, song_id in enumerate(self.events.active().close_song_order())
                }
                songs.sort(key=lambda row: rank.get(row["id"], len(rank)))
            return songs

    def csv_text(
        self,
        event_id: str,
        token: str | None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        songs = (
            self._read_records(event_id, connection=connection)
            if connection is not None
            else self.records(event_id, token)
        )
        output = io.StringIO()
        writer = csv.writer(output)
        fields = (
            "title",
            "requester",
            "status",
            "source_type",
            "source_id",
            "bvid",
            "page",
            "requested_at",
            "played_at",
        )
        writer.writerow(("order", *fields))
        for index, song in enumerate(songs, 1):
            values = [song[field] for field in fields]
            # Prevent spreadsheet formula execution in text and provider fields.
            for column in (0, 1, 4, 5):
                if values[column] and str(values[column])[0] in "=+-@\t\r":
                    values[column] = "'" + str(values[column])
            writer.writerow((index, *values))
        return "\ufeff" + output.getvalue()

    def export(self, event_id: str, token: str | None) -> Response:
        return Response(
            self.csv_text(event_id, token),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="vc-ktv-{event_id}-songs.csv"'
            },
        )
