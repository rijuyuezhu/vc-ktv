"""Guest and administrator cancellation of queued songs."""

import sqlite3

from fastapi import APIRouter, Cookie, HTTPException


def create_cancellation_router(
    *, store, require_admin, require_user, stop_song_processing
):
    router = APIRouter()

    def bail(status: int, message: str):
        raise HTTPException(status_code=status, detail=message)

    @router.post("/api/songs/{song_id}/cancel")
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

    @router.post("/api/admin/songs/{song_id}/cancel")
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

    return router
