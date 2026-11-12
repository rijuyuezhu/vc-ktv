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


def test_shared_ip_join_errors_do_not_lock_out_user_login_or_global_admin(tmp_path):
    app = create_app(
        root=tmp_path / "shared-nat",
        password="pw",
        secret=b"test-shared-nat-failure",
        auto_prepare=False,
    )
    app.state.events.create("Shared", "123456")
    with TestClient(app) as user, TestClient(app) as others:
        assert user.post("/api/join", json={"code": "123456"}).status_code == 200
        assert (
            user.post(
                "/api/users/register",
                json={
                    "username": "Singer",
                    "password": "password-123",
                    "confirm_password": "password-123",
                },
            ).status_code
            == 200
        )
        user.post("/api/users/logout")
        assert user.post("/api/join", json={"code": "123456"}).status_code == 200
        for _ in range(40):
            assert others.post("/api/join", json={"code": "000000"}).status_code == 401
        blocked = others.post("/api/join", json={"code": "000000"})
        assert blocked.status_code == 429
        assert 1 <= int(blocked.headers["retry-after"]) <= 300
        assert "秒后重试" in blocked.json()["detail"]
        assert (
            user.post(
                "/api/users/login",
                json={"username": "Singer", "password": "password-123"},
            ).status_code
            == 200
        )
        assert (
            others.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        assert len(app.state.auth.failed_ips) == 1


def test_shared_ip_many_user_typos_do_not_block_other_users_but_remain_bounded(
    tmp_path,
):
    app = create_app(
        root=tmp_path / "shared-users",
        password="pw",
        secret=b"test-shared-nat-user-failure",
        auto_prepare=False,
    )
    app.state.events.create("Shared", "123456")
    with TestClient(app) as singer, TestClient(app) as others:
        assert singer.post("/api/join", json={"code": "123456"}).status_code == 200
        assert (
            singer.post(
                "/api/users/register",
                json={
                    "username": "Singer",
                    "password": "password-123",
                    "confirm_password": "password-123",
                },
            ).status_code
            == 200
        )
        singer.post("/api/users/logout")
        assert singer.post("/api/join", json={"code": "123456"}).status_code == 200
        assert others.post("/api/join", json={"code": "123456"}).status_code == 200
        for i in range(55):
            assert (
                others.post(
                    "/api/users/login",
                    json={"username": f"other-{i}", "password": "bad"},
                ).status_code
                == 401
            )
        assert (
            singer.post(
                "/api/users/login",
                json={"username": "Singer", "password": "password-123"},
            ).status_code
            == 200
        )
        for i in range(55, 100):
            assert (
                others.post(
                    "/api/users/login",
                    json={"username": f"other-{i}", "password": "bad"},
                ).status_code
                == 401
            )
        blocked = others.post(
            "/api/users/login", json={"username": "unregistered-100", "password": "bad"}
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0
        assert len(app.state.auth.failed_ips) == 1
