"""Read-only event history and shared CSV export for archived media."""

import csv
import io
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Response

from vc_ktv.application.common import bail


def create_event_history_router(events, require_admin):
    router = APIRouter()

    @router.get("/api/admin/events")
    def list_events(vc_admin: str | None = Cookie(default=None)):
        principal = require_admin(vc_admin)
        return {
            "active": events.active_id,
            "events": events.list()
            if principal["role"] == "global"
            else [events.metadata()],
            "role": principal["role"],
            "username": principal.get("username"),
        }

    def event_song_records(event_id: str, token: str | None):
        principal = require_admin(token)
        if principal["role"] == "event" and event_id != events.active_id:
            bail(403, "无权查看其他场次历史")
        with events.lock:
            path = events.database_path(event_id)
            with sqlite3.connect(
                f"file:{quote(str(path), safe='/')}?mode=ro", uri=True
            ) as db:
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
                                  s.page,s.requested_at,s.played_at,s.uploader
                           FROM songs s JOIN guests g ON g.id=s.requester
                           """
                        + ordering
                    )
                ]
            if event_id == events.active_id:
                rank = {
                    song_id: i
                    for i, song_id in enumerate(events.active().close_song_order())
                }
                songs.sort(key=lambda row: rank.get(row["id"], len(rank)))
            return songs

    @router.get("/api/admin/events/{event_id}/songs")
    def event_song_history(event_id: str, vc_admin: str | None = Cookie(default=None)):
        return {"songs": event_song_records(event_id, vc_admin)}

    @router.get("/api/admin/events/{event_id}/songs.csv")
    def export_event_songs(event_id: str, vc_admin: str | None = Cookie(default=None)):
        songs = event_song_records(event_id, vc_admin)
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
        return Response(
            "\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="vc-ktv-{event_id}-songs.csv"'
            },
        )

    return router, export_event_songs
