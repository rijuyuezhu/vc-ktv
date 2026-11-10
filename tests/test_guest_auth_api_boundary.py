"""Guest authentication router keeps its cookie, event and API contracts."""

from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app


def test_guest_auth_router_keeps_signed_cookie_and_archived_event(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"guest-auth-router",
        auto_prepare=False,
    )
    first = app.state.events.create("First event", "001234")
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
