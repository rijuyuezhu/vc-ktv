"""Bound untrusted authentication failure tracking without penalizing normal shared IPs."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app


def test_failed_logins_across_distinct_names_are_limited_per_ip(tmp_path):
    app = create_app(
        root=tmp_path,
        password="pw",
        secret=b"test-bounded-failures",
        auto_prepare=False,
    )
    with TestClient(app) as client:
        for index in range(40):
            response = client.post(
                "/api/admin/login",
                json={"username": f"unregistered-{index}", "password": "wrong"},
            )
            assert response.status_code == 401
        assert (
            client.post(
                "/api/admin/login", json={"username": "another", "password": "wrong"}
            ).status_code
            == 429
        )
        assert len(app.state.auth.failed_login) == 40
        assert len(app.state.auth.failed_ips) == 1


def test_failure_window_expires_and_keys_evict(tmp_path):
    app = create_app(
        root=tmp_path,
        password="pw",
        secret=b"test-bounded-failures",
        auto_prepare=False,
    )
    auth = app.state.auth
    with patch("vc_ktv.application.auth.time_now", return_value=1000):
        for i in range(2200):
            auth.note_failed_attempt(
                auth.failed_login, (f"ip-{i}", f"user-{i}"), f"ip-{i}"
            )
        assert len(auth.failed_login) <= 2048
        assert len(auth.failed_ips) <= 2048
    with patch("vc_ktv.application.auth.time_now", return_value=1400):
        auth.check_attempt(auth.failed_login, ("ip-0", "user-0"), "ip-0")
        assert auth.failed_login == {}
        assert auth.failed_ips == {}


def test_normal_admin_login_and_restricted_input_lengths(tmp_path):
    app = create_app(
        root=tmp_path,
        password="pw",
        secret=b"test-bounded-failures",
        auto_prepare=False,
    )
    with TestClient(app) as client:
        assert (
            client.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        assert (
            client.post(
                "/api/admin/login", json={"username": "x" * 25, "password": "pw"}
            ).status_code
            == 422
        )
        assert (
            client.post("/api/admin/login", json={"password": "x" * 1025}).status_code
            == 422
        )
        assert client.post("/api/admin/login", json={"password": ""}).status_code == 422
