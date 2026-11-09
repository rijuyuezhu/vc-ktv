"""Read-only event song history and canonical CSV archive formatting."""

import csv
import io
import sqlite3
from urllib.parse import quote

from fastapi import Response

from vc_ktv.application.errors import bail


class EventArchiveService:
    def __init__(self, events, auth):
        self.events = events
        self.auth = auth

    def records(self, event_id: str, token: str | None) -> list[dict]:
        principal = self.auth.require_admin(token)
        if principal["role"] == "event" and event_id != self.events.active_id:
            bail(403, "无权查看其他场次历史")
        with self.events.lock:
            path = self.events.database_path(event_id)
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
            if event_id == self.events.active_id:
                rank = {
                    song_id: i
                    for i, song_id in enumerate(self.events.active().close_song_order())
                }
                songs.sort(key=lambda row: rank.get(row["id"], len(rank)))
            return songs

    def csv_text(self, event_id: str, token: str | None) -> str:
        songs = self.records(event_id, token)
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
