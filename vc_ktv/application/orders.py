"""Atomic song admission shared by the Bilibili and NetEase request paths."""

import sqlite3

from vc_ktv.application.errors import bail
from vc_ktv.core.store import time_now
from vc_ktv.infra.disk_budget import DiskBudgetExceeded
from vc_ktv.sources.base import SongInfo

MAX_ACTIVE_SONGS = 1000
MAX_USER_SONGS = 20


class OrderService:
    """Admission checks use Store.lock; submit holds it through insertion."""

    def __init__(self, store, events):
        self.store = store
        self.events = events

    def check_room_capacity(self) -> None:
        count = self.store.db.execute(
            "SELECT COUNT(*) FROM songs WHERE status IN ('rendering','ready','playing')"
            " OR (status='waiting' AND NOT "
            "(prep_state='unavailable' AND prep_attempts>=3))"
        ).fetchone()[0]
        if count >= MAX_ACTIVE_SONGS:
            bail(409, "本场待处理歌曲已满，请稍后再点歌")
        try:
            self.store.disk_budget.check()
        except DiskBudgetExceeded as error:
            bail(507, str(error))

    def check_guest_capacity(self, guest_id: str) -> None:
        count = self.store.db.execute(
            "SELECT COUNT(*) FROM songs WHERE requester=? AND "
            "(status IN ('rendering','ready','playing') OR "
            "(status='waiting' AND NOT "
            "(prep_state='unavailable' AND prep_attempts>=3)))",
            (guest_id,),
        ).fetchone()[0]
        if count >= MAX_USER_SONGS:
            bail(409, "每人最多保留二十首待唱歌曲")

    def check_new_order(self, guest_id: str) -> None:
        if self.store.settings()["orders_paused"]:
            bail(409, "本场已暂停点歌")
        self.check_room_capacity()
        self.check_guest_capacity(guest_id)

    def submit(
        self,
        guest_id: str,
        event_id: str,
        song: SongInfo,
        *,
        changed_message: str,
    ) -> int:
        """Recheck admission and insert in one Store transaction after external lookup."""
        with self.store.lock:
            if self.events.active_id != event_id:
                bail(409, changed_message)
            self.check_new_order(guest_id)
            try:
                cur = self.store.db.execute(
                    """INSERT INTO songs(
                        bvid,page,title,uploader,cover,duration,requester,requested_at,
                        source_type,source_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    song.db_values(guest_id, time_now()),
                )
                self.store.db.commit()
            except sqlite3.Error:
                self.store.db.rollback()
                raise
            return cur.lastrowid
