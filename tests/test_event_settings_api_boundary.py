"""Admin playback settings retain their scope and request validation."""

from fastapi.testclient import TestClient

from vc_ktv.api.event_admin import PlaybackSettings
from vc_ktv.api.event_admin import PlaybackSettings as RouterPlaybackSettings
from vc_ktv.application.factory import create_app


def test_settings_router_preserves_model_and_admin_scope(tmp_path):
    assert PlaybackSettings is RouterPlaybackSettings
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"settings-router-contract",
        auto_prepare=False,
    )
    settings_paths = app.openapi()["paths"]["/api/admin/settings"]
    assert {"get", "put"} <= settings_paths.keys()
    with TestClient(app) as admin, TestClient(app) as outsider:
        assert outsider.get("/api/admin/settings").status_code == 401
        assert (
            outsider.put(
                "/api/admin/settings", json={"orders_paused": True}
            ).status_code
            == 401
        )
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert (
            admin.post("/api/admin/events", json={"name": "First"}).status_code == 200
        )
        before = admin.get("/api/admin/settings").json()
        updated = admin.put(
            "/api/admin/settings",
            json={
                "orders_paused": True,
                "queue_policy": "fcfs",
                "tv_html_overlay": False,
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["orders_paused"] is True
        assert updated.json()["queue_policy"] == "fcfs"
        assert updated.json()["tv_html_overlay"] is False
        after = admin.get("/api/admin/settings").json()
        assert after == updated.json()
        assert after["max_bandwidth_mbps"] == before["max_bandwidth_mbps"]
        assert (
            admin.put("/api/admin/settings", json={"video_quality": 1440}).status_code
            == 422
        )
        assert admin.get("/api/admin/settings").json() == after
