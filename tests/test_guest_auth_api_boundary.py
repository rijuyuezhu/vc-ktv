"""Guest authentication router keeps its cookie, event and API contracts."""

from fastapi.testclient import TestClient

from vc_ktv.api.guest_auth import JoinEvent as RouterJoinEvent
from vc_ktv.api.guest_auth import RegisterUser as RouterRegisterUser
from vc_ktv.api.guest_auth import UserLogin as RouterUserLogin
from vc_ktv.app import JoinEvent, RegisterUser, UserLogin, create_app


def test_guest_auth_router_keeps_signed_cookie_and_archived_event(tmp_path):
    assert JoinEvent is RouterJoinEvent
    assert RegisterUser is RouterRegisterUser
    assert UserLogin is RouterUserLogin
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"guest-auth-router",
        auto_prepare=False,
    )
    first = app.state.events.create("First event", "001234")
    paths = {
        (path, method)
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    assert {
        ("/api/entry", "get"),
        ("/api/join", "post"),
        ("/api/users/register", "post"),
        ("/api/users/login", "post"),
        ("/api/users/logout", "post"),
    } <= paths
    with TestClient(app) as guest, TestClient(app) as admin:
        assert guest.get("/api/entry").json()["authenticated"] is False
        assert (
            guest.post(
                "/api/users/register",
                json={
                    "username": "Singer",
                    "password": "password-123",
                    "confirm_password": "password-123",
                },
            ).status_code
            == 401
        )
        assert guest.post("/api/join", json={"code": "001234"}).status_code == 200
        assert guest.cookies.get("vc_join") is not None
        assert (
            guest.post(
                "/api/users/register",
                json={
                    "username": "Singer",
                    "password": "password-123",
                    "confirm_password": "password-123",
                },
            ).status_code
            == 200
        )
        assert guest.cookies.get("vc_guest")
        assert guest.get("/api/entry").json()["username"] == "Singer"
        assert guest.post("/api/users/logout").json() == {"ok": True}
        assert guest.get("/api/entry").json()["joined"] is False
        assert guest.post("/api/join", json={"code": "001234"}).status_code == 200
        assert (
            guest.post(
                "/api/users/login",
                json={"username": "Singer", "password": "password-123"},
            ).status_code
            == 200
        )
        assert guest.get("/api/entry").json()["authenticated"] is True

        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert admin.post("/api/admin/events/close").status_code == 200
        archive = guest.get("/api/entry").json()
        assert archive["event"] is None
        assert archive["authenticated"] is False
        assert archive["archived_event"]["id"] == first["id"]
        assert archive["archived_event"]["username"] == "Singer"
