from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.accounts import password_hash, verify_password
from vc_ktv.app import create_app


def setup_room(tmp_path):
    app = create_app(
        root=tmp_path / "event-users",
        password="admin-test",
        secret=b"isolated-users-test-secret",
        auto_prepare=False,
    )
    app.state.events.create("今天的 KTV", "000123")
    return app


def test_event_code_register_login_and_logout(tmp_path):
    app = setup_room(tmp_path)
    with TestClient(app) as alice, TestClient(app) as bob:
        entry = alice.get("/api/entry").json()
        assert entry["event"]["name"] == "今天的 KTV"
        assert entry["joined"] is False
        assert entry["authenticated"] is False
        assert alice.get("/api/state").status_code == 401
        assert (
            alice.get("/api/preview", params={"bvid": "BV0000000001"}).status_code
            == 401
        )
        assert (
            alice.post("/api/songs", json={"bvid": "BV0000000001"}).status_code == 401
        )
        assert (
            alice.post(
                "/api/users/register",
                json={
                    "username": "Alice",
                    "password": "password1",
                    "confirm_password": "password1",
                },
            ).status_code
            == 401
        )

        assert alice.post("/api/join", json={"code": "000124"}).status_code == 401
        assert alice.post("/api/join", json={"code": "000123"}).status_code == 200
        assert alice.get("/api/entry").json()["joined"]
        assert (
            alice.post(
                "/api/users/register",
                json={
                    "username": "Alice",
                    "password": "password1",
                    "confirm_password": "password2",
                },
            ).status_code
            == 400
        )
        assert (
            alice.post(
                "/api/users/register",
                json={
                    "username": "Alice",
                    "password": "short",
                    "confirm_password": "short",
                },
            ).status_code
            == 400
        )
        assert (
            alice.post(
                "/api/users/register",
                json={
                    "username": "Alice",
                    "password": "password1",
                    "confirm_password": "password1",
                },
            ).status_code
            == 200
        )
        entry = alice.get("/api/entry").json()
        assert entry["authenticated"] is True and entry["username"] == "Alice"
        assert alice.get("/api/state").status_code == 200

        with app.state.store.lock:
            stored = app.state.store.db.execute(
                "SELECT password_hash FROM users WHERE username='Alice'"
            ).fetchone()[0]
        assert stored != "password1" and "password1" not in stored
        assert verify_password("password1", stored)
        assert not verify_password("wrong", stored)

        assert bob.post("/api/join", json={"code": "000123"}).status_code == 200
        assert (
            bob.post(
                "/api/users/register",
                json={
                    "username": "alice",
                    "password": "password2",
                    "confirm_password": "password2",
                },
            ).status_code
            == 409
        )
        assert (
            bob.post(
                "/api/users/login", json={"username": "Alice", "password": "wrong"}
            ).status_code
            == 401
        )
        assert (
            bob.post(
                "/api/users/login", json={"username": "Alice", "password": "password1"}
            ).status_code
            == 200
        )
        assert bob.get("/api/entry").json()["authenticated"]

        assert alice.post("/api/users/logout").status_code == 200
        assert alice.get("/api/entry").json()["joined"] is False
        assert alice.get("/api/state").status_code == 401


def test_users_are_temporary_and_bound_to_the_event(tmp_path):
    app = setup_room(tmp_path)
    with TestClient(app) as admin, TestClient(app) as alice:
        join_and_register(alice, "Alice", "000123")
        assert (
            admin.post("/api/admin/login", json={"password": "admin-test"}).status_code
            == 200
        )
        assert admin.post("/api/admin/events/close").status_code == 200
        new = admin.post(
            "/api/admin/events", json={"name": "明天的 KTV", "code": "000123"}
        )
        assert new.status_code == 200
        assert alice.get("/api/entry").json()["authenticated"] is False
        assert alice.get("/api/state").status_code == 401
        assert (
            alice.post(
                "/api/users/login",
                json={"username": "Alice", "password": "test-login-password"},
            ).status_code
            == 401
        )
        assert alice.post("/api/join", json={"code": "000123"}).status_code == 200
        assert (
            alice.post(
                "/api/users/login",
                json={"username": "Alice", "password": "test-login-password"},
            ).status_code
            == 401
        )
        join_and_register(alice, "Alice", "000123")
        assert alice.get("/api/entry").json()["authenticated"] is True


def test_invite_rate_limit_and_password_hash_validation(tmp_path):
    app = setup_room(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/join", json={"code": "000123"}).status_code == 200
    assert not verify_password("x", "not-a-hash")
    stored = password_hash("example-password")
    assert verify_password("example-password", stored)
    with TestClient(app) as attacker:
        for _ in range(10):
            assert (
                attacker.post("/api/join", json={"code": "999999"}).status_code == 401
            )
        assert attacker.post("/api/join", json={"code": "000123"}).status_code == 429
