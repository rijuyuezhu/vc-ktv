"""Both admin and guests receive BV numbers for copy actions in all song views."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app


def test_queue_current_detail_and_played_history_expose_original_bv(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"copy-bv-key",
        auto_prepare=False,
    )
    app.state.events.create("Party", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "《A song》",
            "uploader": "UP",
            "cover": "",
            "duration": 40,
        }

    app.state.lookup = lookup
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "Singer")
        sid = guest.post("/api/songs", json={"bvid": "BV0000000009"}).json()["id"]
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert guest.get("/api/state").json()["waiting"][0]["bvid"] == "BV0000000009"
        assert (
            admin.get("/api/admin/state").json()["waiting"][0]["bvid"] == "BV0000000009"
        )
        assert guest.get(f"/api/songs/{sid}/detail").json()["bvid"] == "BV0000000009"
        assert admin.get(f"/api/songs/{sid}/detail").json()["bvid"] == "BV0000000009"
        with app.state.store.lock:
            app.state.store.db.execute(
                "UPDATE songs SET status='played',prep_state='original',played_at=5,media_file=? WHERE id=?",
                ("original.mp4", sid),
            )
            (app.state.store.media / "original.mp4").write_bytes(b"video")
            app.state.store.db.commit()
        assert (
            guest.get("/api/songs/played").json()["songs"][0]["bvid"] == "BV0000000009"
        )
        assert (
            admin.get("/api/admin/played").json()["songs"][0]["bvid"] == "BV0000000009"
        )
