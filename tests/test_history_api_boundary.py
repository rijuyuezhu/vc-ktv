"""Read-only song routes preserve their URL and authentication contracts."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.application.factory import create_app


def test_history_and_detail_routes(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"example",
        auto_prepare=False,
    )
    app.state.events.create("Test event", "123456")
    paths = app.openapi()["paths"]
    assert {
        "/api/songs/history",
        "/api/songs/played",
        "/api/songs/{song_id}/detail",
    } <= paths.keys()
    with TestClient(app) as guest, TestClient(app) as outsider:
        assert outsider.get("/api/songs/history").status_code == 401
        assert outsider.get("/api/songs/played").status_code == 401
        assert outsider.get("/api/songs/42/detail").status_code == 401
        join_and_register(guest, "Singer")
        assert guest.get("/api/songs/history").json() == {
            "songs": [],
            "archived": False,
        }
        assert guest.get("/api/songs/played").json() == {"songs": []}
        assert guest.get("/api/songs/42/detail").status_code == 404
