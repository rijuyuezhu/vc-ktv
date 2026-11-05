"""Retry a failed media preparation without forcing a fresh guest order."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app


def test_admin_retry_failed_song_preserves_source_and_fairness(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"retry-failed-media",
        auto_prepare=False,
    )
    app.state.events.create("Test", "123456")

    async def lookup(song_id):
        return {
            "source": "netease",
            "source_id": str(song_id),
            "title": "沦为尘埃",
            "uploader": "歌手",
            "cover": "",
            "duration": 180,
        }

    app.state.netease_lookup = lookup
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "歌手")
        song_id = guest.post("/api/netease/songs", json={"ref": "3434373208"}).json()[
            "id"
        ]
        with app.state.store.lock:
            app.state.store.db.execute(
                "UPDATE songs SET prep_state='unavailable',prep_attempts=3,"
                "prep_error='网易云媒体下载失败（HTTP 403）',prep_retry_at=0 WHERE id=?",
                (song_id,),
            )
            app.state.store.db.commit()
        endpoint = f"/api/admin/songs/{song_id}/download/retry"
        assert guest.post(endpoint).status_code == 401
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        assert admin.post(endpoint).status_code == 200
        assert admin.post(endpoint).status_code == 409
        row = app.state.store.rows("SELECT * FROM songs WHERE id=?", (song_id,))[0]
        assert (row["status"], row["prep_state"], row["prep_attempts"]) == (
            "waiting",
            "pending",
            0,
        )
        assert row["source_type"] == "netease" and row["source_id"] == "3434373208"
        assert row["prep_error"] == "" and row["prep_retry_at"] == 0
        assert admin.post("/api/admin/songs/999999/download/retry").status_code == 404
