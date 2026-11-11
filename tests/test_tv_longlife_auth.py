"""The main screen renews credentials without leaking its bearer in URLs."""

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.test_tv_primary import admin_login, app_at, pair_link


def test_primary_renews_past_eight_hours_using_bearer_header(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("TV all night", "123456")
    clock = [1_790_000_000]
    with (
        patch("vc_ktv.application.auth.time_now", side_effect=lambda: clock[0]),
        patch("vc_ktv.application.tv.time_now", side_effect=lambda: clock[0]),
        TestClient(app, base_url="https://testserver") as admin,
        TestClient(app, base_url="https://testserver") as tv,
    ):
        admin_login(admin)
        pair = pair_link(admin)
        response = tv.post("/api/tv/activate", json={"pair": pair})
        assert response.status_code == 200
        bearer = response.json()["token"]
        assert "vc_tv_events=" in response.headers["set-cookie"]
        for flag in ("httponly", "secure", "samesite=strict", "path=/api/tv/events"):
            assert flag in response.headers["set-cookie"].lower()
        assert (
            tv.get(
                "/api/tv/state", headers={"Authorization": f"Bearer {bearer}"}
            ).status_code
            == 200
        )
        assert tv.get("/api/tv/state", params={"token": bearer}).status_code == 200
        clock[0] += 7 * 3600 + 50 * 60
        renewed = tv.post(
            "/api/tv/renew", headers={"Authorization": f"Bearer {bearer}"}
        )
        assert renewed.status_code == 200
        next_bearer = renewed.json()["token"]
        assert next_bearer != bearer
        clock[0] += 20 * 60
        assert (
            tv.get(
                "/api/tv/state", headers={"Authorization": f"Bearer {bearer}"}
            ).status_code
            == 401
        )
        assert (
            tv.get(
                "/api/tv/state", headers={"Authorization": f"Bearer {next_bearer}"}
            ).status_code
            == 200
        )
        assert (
            tv.post(
                "/api/tv/next", headers={"Authorization": f"Bearer {next_bearer}"}
            ).status_code
            == 200
        )
        assert (
            tv.post(
                "/api/tv/renew", headers={"Authorization": "Basic invalid"}
            ).status_code
            == 401
        )
        assert admin.post("/api/admin/tv/revoke").status_code == 200
        assert (
            tv.get(
                "/api/tv/state", headers={"Authorization": f"Bearer {next_bearer}"}
            ).status_code
            == 401
        )
        assert (
            tv.post(
                "/api/tv/renew", headers={"Authorization": f"Bearer {next_bearer}"}
            ).status_code
            == 401
        )


def test_tv_browser_uses_header_authorization_and_cookie_sse_only():
    source = (Path(__file__).resolve().parents[1] / "static/tv.js").read_text()
    assert 'new EventSource("/api/tv/events")' in source
    assert 'headers: { Authorization: "Bearer " + (token || "") }' in source
    assert 'api("/api/tv/renew", "POST")' in source
    assert '"?token="' not in source
    assert '"token="' not in source
