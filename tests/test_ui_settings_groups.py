"""Separate settings forms must not overwrite unrelated unsaved preferences."""

from fastapi.testclient import TestClient

from vc_ktv.app import create_app


def test_settings_video_and_queue_partial_updates_preserve_other_group(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"ui-settings-test",
        auto_prepare=False,
    )
    app.state.events.create("Room", "123456")
    with TestClient(app) as client:
        assert (
            client.post("/api/admin/login", json={"password": "admin"}).status_code
            == 200
        )
        before = client.get("/api/admin/settings").json()
        assert (
            client.put(
                "/api/admin/settings",
                json={
                    "subtitle_style": "large",
                    "video_quality": 480,
                    "max_bandwidth_mbps": 4.0,
                    "tv_html_overlay": False,
                },
            ).status_code
            == 200
        )
        video = client.get("/api/admin/settings").json()
        assert video["queue_policy"] == before["queue_policy"]
        assert video["orders_paused"] == before["orders_paused"]
        assert video["allow_unsubtitled"] == before["allow_unsubtitled"]
        assert video["subtitle_style"] == "large"
        assert video["max_bandwidth_mbps"] == 4.0

        assert (
            client.put(
                "/api/admin/settings",
                json={
                    "queue_policy": "fcfs",
                    "allow_unsubtitled": True,
                    "orders_paused": True,
                },
            ).status_code
            == 200
        )
        queue = client.get("/api/admin/settings").json()
        assert queue["queue_policy"] == "fcfs"
        assert queue["orders_paused"] is True
        assert queue["allow_unsubtitled"] is True
        assert queue["video_quality"] == 480
        assert queue["subtitle_style"] == "large"
        assert queue["max_bandwidth_mbps"] == 4.0
        assert queue["tv_html_overlay"] is False
