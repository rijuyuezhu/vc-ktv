"""Event-scoped SQLite store, virtual replay assets, fair queue and state."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal

from vc_ktv.core.titles import title_key
from vc_ktv.infra.disk_budget import EventDiskBudget
from vc_ktv.rendering.subtitle_styles import StylePreset


def time_now():
    return int(time.time())


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.media = root / "media"
        self.media.mkdir(exist_ok=True)
        self.disk_budget = EventDiskBudget(root)
        self.db = sqlite3.connect(root / "room.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS guests(
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                plays INTEGER NOT NULL DEFAULT 0,
                seconds INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY REFERENCES guests(id),
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS songs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT NOT NULL, page INTEGER NOT NULL,
                title TEXT NOT NULL, uploader TEXT NOT NULL,
                cover TEXT NOT NULL, duration INTEGER NOT NULL,
                requester TEXT NOT NULL REFERENCES guests(id),
                requested_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
                media_file TEXT, lyrics TEXT NOT NULL DEFAULT '',
                played_at INTEGER);
            CREATE TABLE IF NOT EXISTS room_state(
                name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS event_admins(
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1);
        """)
        # Scheduling offsets are deliberately separate from real performance
        # statistics: late arrivals must not appear to have sung before joining.
        guest_columns = {row[1] for row in self.db.execute("PRAGMA table_info(guests)")}
        # fair_seconds is a legacy database column retained for compatibility,
        # but is never read by the song-count-only scheduler.
        for name in ("fair_offset", "fair_seconds"):
            if name not in guest_columns:
                self.db.execute(
                    f"ALTER TABLE guests ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(songs)")}
        if "source_file" not in columns:
            self.db.execute("ALTER TABLE songs ADD COLUMN source_file TEXT")
            self.db.execute(
                "UPDATE songs SET source_file=media_file WHERE media_file IS NOT NULL"
            )
        if "render_error" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN render_error TEXT NOT NULL DEFAULT ''"
            )
        self.db.execute(
            "UPDATE songs SET status='ready',render_error='制作中断，可重新尝试' WHERE status='rendering' AND source_file IS NOT NULL"
        )
        if "prep_state" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN prep_state TEXT NOT NULL DEFAULT 'pending'"
            )
            self.db.execute(
                "UPDATE songs SET prep_state='manual' "
                "WHERE media_file IS NOT NULL OR status != 'waiting'"
            )
        if "prep_error" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN prep_error TEXT NOT NULL DEFAULT ''"
            )
        if "prep_attempts" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN prep_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "prep_retry_at" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN prep_retry_at INTEGER NOT NULL DEFAULT 0"
            )
        if "lyric_choice" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN lyric_choice TEXT NOT NULL DEFAULT ''"
            )
        if "lyric_source" not in columns:
            self.db.execute(
                "ALTER TABLE songs ADD COLUMN lyric_source TEXT NOT NULL DEFAULT ''"
            )
        for column, ddl in (
            ("fair_exempt", "INTEGER NOT NULL DEFAULT 0"),
            ("skip_subtitles", "INTEGER NOT NULL DEFAULT 0"),
            ("prep_stage", "TEXT NOT NULL DEFAULT ''"),
            ("prep_stage_at", "INTEGER NOT NULL DEFAULT 0"),
            ("prep_timing", "TEXT NOT NULL DEFAULT '{}'"),
            ("priority_next", "INTEGER NOT NULL DEFAULT 0"),
            ("replay_of", "INTEGER"),
            ("lyric_timing_mode", "TEXT NOT NULL DEFAULT 'align'"),
            ("provider_lrc", "TEXT NOT NULL DEFAULT ''"),
            ("source_lrc_raw", "TEXT NOT NULL DEFAULT ''"),
            ("lyric_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("source_type", "TEXT NOT NULL DEFAULT 'bilibili'"),
            ("source_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                self.db.execute(f"ALTER TABLE songs ADD COLUMN {column} {ddl}")
        if "archive_order" not in columns:
            self.db.execute("ALTER TABLE songs ADD COLUMN archive_order INTEGER")
        self.db.commit()

    def rows(self, sql, args=()):
        # The connection is shared by HTTP and AutoPrep; never expose a
        # different thread's uncommitted writes through this read helper.
        with self.lock:
            return [dict(x) for x in self.db.execute(sql, args).fetchall()]

    def replay_chain(self, song):
        """Return this turn and its ancestors within the current Store/event."""
        with self.lock:
            current = dict(song)
            if "replay_of" not in current:
                row = self.db.execute(
                    "SELECT * FROM songs WHERE id=?", (current["id"],)
                ).fetchone()
                if row:
                    current = dict(row)
            chain, visited = [], set()
            while current and current["id"] not in visited:
                chain.append(current)
                visited.add(current["id"])
                parent_id = current.get("replay_of")
                if parent_id is None:
                    break
                row = self.db.execute(
                    "SELECT * FROM songs WHERE id=?", (parent_id,)
                ).fetchone()
                current = dict(row) if row else None
            return chain

    def replay_assets(self, song):
        """Resolve effective media paths, without creating hard/soft links."""
        resolved = dict(song)
        for ancestor in self.replay_chain(song):
            for name in ("media_file", "source_file"):
                if not resolved.get(name) and ancestor.get(name):
                    resolved[name] = ancestor[name]
            if resolved.get("media_file") and resolved.get("source_file"):
                break
        # Legacy songs may only have a rendered video. Their replays used
        # to treat that video as the source too; preserve that behavior.
        if resolved.get("replay_of") is not None and not resolved.get("source_file"):
            resolved["source_file"] = resolved.get("media_file")
        return resolved

    def current(self):
        rows = self.rows(
            "SELECT * FROM songs WHERE status='playing' ORDER BY id LIMIT 1"
        )
        return rows[0] if rows else None

    def songs(self):
        rows = self.rows("""
            SELECT s.id,s.bvid,s.page,s.source_type,s.source_id,
                   s.title,s.uploader,s.cover,s.duration,
                   s.requested_at,s.status,(s.media_file IS NOT NULL OR s.replay_of IS NOT NULL) AS has_media,s.render_error,
                   s.prep_state,s.prep_error,s.prep_attempts,s.prep_retry_at,
                   s.skip_subtitles,s.prep_stage,s.prep_stage_at,s.priority_next,s.replay_of,
                   s.lyric_choice != '' AS lyric_selected,s.lyric_source,
                   s.lyric_timing_mode,s.lyric_offset_ms,
                   g.name AS nickname,g.id AS requester_id,s.lyrics != '' AS has_lyrics
            FROM songs s JOIN guests g ON g.id=s.requester
            WHERE s.status IN ('waiting','rendering','ready','playing')
            ORDER BY s.requested_at,s.id
        """)
        return [{**song, "lyric_query": title_key(song["title"])} for song in rows]

    def settings(self):
        values = {
            row["name"]: row["value"]
            for row in self.rows(
                "SELECT name,value FROM room_state "
                "WHERE name IN ('queue_policy','allow_unsubtitled','subtitle_style','video_quality','max_bandwidth_mbps','orders_paused','tv_html_overlay')"
            )
        }
        return {
            "queue_policy": values.get("queue_policy", "fair"),
            "allow_unsubtitled": values.get("allow_unsubtitled", "0") == "1",
            "subtitle_style": values.get("subtitle_style", "classic"),
            "video_quality": int(values.get("video_quality", "720")),
            "max_bandwidth_mbps": float(values.get("max_bandwidth_mbps", "3")),
            "orders_paused": values.get("orders_paused", "0") == "1",
            "tv_html_overlay": values.get("tv_html_overlay", "1") == "1",
        }

    def playback_mode(self):
        row = self.db.execute(
            "SELECT value FROM room_state WHERE name='playback_mode'"
        ).fetchone()
        return row[0] if row and row[0] in ("browser", "tv", "manual") else "tv"

    def save_settings(
        self,
        queue_policy: str | None,
        allow_unsubtitled: bool | None,
        subtitle_style: StylePreset | None = None,
        video_quality: Literal[720, 480] | None = None,
        max_bandwidth_mbps: float | None = None,
        orders_paused: bool | None = None,
        tv_html_overlay: bool | None = None,
    ):
        # Optional values are updates, not resets; legacy admin clients omit them.
        updates = {}
        if queue_policy is not None:
            updates["queue_policy"] = queue_policy
        if allow_unsubtitled is not None:
            updates["allow_unsubtitled"] = "1" if allow_unsubtitled else "0"
        for key, value in (
            ("subtitle_style", subtitle_style),
            ("video_quality", video_quality),
            ("max_bandwidth_mbps", max_bandwidth_mbps),
            ("orders_paused", None if orders_paused is None else int(orders_paused)),
            (
                "tv_html_overlay",
                None if tv_html_overlay is None else int(tv_html_overlay),
            ),
        ):
            if value is not None:
                updates[key] = str(value)
        with self.lock:
            try:
                self.db.executemany(
                    "INSERT INTO room_state(name,value) VALUES(?,?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    updates.items(),
                )
                self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise
            return self.settings()

    @staticmethod
    def playable(song, settings):
        return (
            song["status"] == "ready"
            and song["has_media"]
            and (
                settings["allow_unsubtitled"]
                or song.get("skip_subtitles", 0)
                or song["prep_state"] in ("done", "manual")
            )
        )

    def fair_queue(self, pool=None, settings=None, *, persist=False):
        """Order ready songs with at most two turns of accumulated catch-up.

        Preview is read-only; persist virtual credit only when claiming a song.
        Already completed performances remain a separate real-world statistic.
        """
        if pool is None:
            pool = self.songs()
        if settings is None:
            settings = self.settings()
        ready = [s for s in pool if self.playable(s, settings)]
        # The single admin-selected next song precedes ordinary fair/FCFS ordering.
        priority = [s for s in ready if s["priority_next"]]
        ready = [s for s in ready if not s["priority_next"]]
        if settings["queue_policy"] == "fcfs":
            return priority + ready  # songs() already orders by requested_at/id.
        users = {x["id"]: x for x in self.rows("SELECT * FROM guests")}
        # Simulate future turns so the displayed order reflects selection policy.
        tally = {k: u["plays"] + u["fair_offset"] for k, u in users.items()}
        # During playback, project the ordinary finish/skip turn before
        # predicting the next claim. Preview must never persist that turn.
        current = self.current() if not persist else None
        if current and not current["fair_exempt"]:
            tally[current["requester"]] += 1
        # Only people with a playable song can compete for a turn. Historical
        # inactivity must not accumulate unlimited "missing" rounds.
        # Admin-chosen priority songs are fictitious admin turns. They cannot
        # advance anybody's virtual count, including via floor normalization.
        active = {song["requester_id"] for song in ready}
        if active:
            floor = max(0, max(tally[user] for user in active) - 2)
            for user in active:
                if tally[user] < floor:
                    delta = floor - tally[user]
                    tally[user] = floor
                    if persist and not priority:
                        self.db.execute(
                            "UPDATE guests SET fair_offset=fair_offset+? WHERE id=?",
                            (delta, user),
                        )
        last = self.db.execute(
            "SELECT value FROM room_state WHERE name='last_requester'"
        ).fetchone()
        previous = last[0] if last else ""
        if current and not current["fair_exempt"]:
            previous = current["requester"]
        output = []
        while ready:
            head = {}
            for s in ready:
                head.setdefault(s["requester_id"], s)
            chosen = min(
                head.values(),
                key=lambda s: (
                    tally[s["requester_id"]],
                    s["requester_id"] == previous,
                    s["requested_at"],
                    s["id"],
                ),
            )
            output.append(chosen)
            tally[chosen["requester_id"]] += 1
            previous = chosen["requester_id"]
            ready.remove(chosen)
        return priority + output

    @staticmethod
    def downloadable(song):
        return (
            song["status"] in ("ready", "playing", "played", "skipped")
            and song["prep_state"] in ("done", "manual", "original")
            and bool(song.get("has_media") or song.get("media_file"))
        )

    def close_song_order(self):
        """Freeze actual turns, then the fair preview, then cancellations."""
        songs = self.rows(
            "SELECT id,status,played_at,requested_at FROM songs ORDER BY requested_at,id"
        )
        finished = sorted(
            (s for s in songs if s["status"] in ("played", "skipped")),
            key=lambda s: (s["played_at"] or 0, s["id"]),
        )
        active = [s for s in songs if s["status"] == "playing"]
        # Predict the full unplayed order, including tracks still preparing.
        # Pretend all pending tracks are playable, without persisting fair credit.
        unplayed_pool = [
            {**s, "status": "ready", "has_media": 1, "prep_state": "done"}
            for s in self.songs()
            if s["status"] in ("ready", "waiting", "rendering")
        ]
        predicted = self.fair_queue(unplayed_pool)
        cancelled = [s for s in songs if s["status"] == "cancelled"]
        ordered = [
            *(s["id"] for s in finished),
            *(s["id"] for s in active),
            *(s["id"] for s in predicted),
            *(s["id"] for s in cancelled),
        ]
        # Cover any legacy status with a stable position as well.
        included = set(ordered)
        ordered.extend(s["id"] for s in songs if s["id"] not in included)
        return ordered

    def claim_next(self):
        """Claim the first playable turn, leaving an existing player untouched.

        Caller holds the relevant lock: event-scoped for HTTP/TV routes,
        Store.lock when used directly.
        """
        if not self.current():
            try:
                queue = self.fair_queue(persist=True)
                if queue:
                    song = queue[0]
                    # Preserve an admin-pinned turn's fair-count exemption.
                    self.db.execute(
                        "UPDATE songs SET status='playing',priority_next=0,fair_exempt=? WHERE id=?",
                        (int(bool(song["priority_next"])), song["id"]),
                    )
                    self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise

    def finish_current(self, song_id: int, *, skipped: bool = False) -> int | None:
        """Settle a turn and return ready count, or None on a stale claim.

        Caller holds the relevant lock across settlement and response.
        Real performances and skipped turns have distinct counters; admin
        priority turns are exempt from both. HTTP errors belong to routes.
        """
        song = self.current()
        if not song or song["id"] != song_id:
            return None
        try:
            self.db.execute(
                "UPDATE songs SET status=?,played_at=? WHERE id=?",
                ("skipped" if skipped else "played", time_now(), song_id),
            )
            if not song["fair_exempt"]:
                if skipped:
                    # A skipped ordinary turn consumes only virtual song credit.
                    self.db.execute(
                        "UPDATE guests SET fair_offset=fair_offset+1 WHERE id=?",
                        (song["requester"],),
                    )
                else:
                    self.db.execute(
                        "UPDATE guests SET plays=plays+1,seconds=seconds+? WHERE id=?",
                        (song["duration"], song["requester"]),
                    )
                self.db.execute(
                    "INSERT INTO room_state(name,value) VALUES('last_requester',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (song["requester"],),
                )
            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            raise
        return len(self.fair_queue())

    def state(self):
        with self.lock:
            pool = self.songs()
            for song in pool:
                song["downloadable"] = self.downloadable(song)
            settings = self.settings()
            return {
                "current": next((s for s in pool if s["status"] == "playing"), None),
                "queue": self.fair_queue(pool, settings),
                "waiting": [
                    {**s, "awaiting_subtitles": s["status"] == "ready"}
                    for s in pool
                    if s["status"] in ("waiting", "rendering")
                    or (s["status"] == "ready" and not self.playable(s, settings))
                ],
                "guests": self.rows(
                    "SELECT name,plays,seconds FROM guests ORDER BY name"
                ),
                "playback_mode": self.playback_mode(),
                "server_time": time_now(),
            }
