"""Exactly one controlling TV per event, with administrator priority."""

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from tests.test_event_admins import create_account, event_login
from vc_ktv.app import create_app


def app_at(root):
    return create_app(
        root=root, password="pw", secret=b"unique-primary-test-key", auto_prepare=False
    )


def admin_login(client):
    assert client.post("/api/admin/login", json={"password": "pw"}).status_code == 200


def pair_link(admin, *, replace=False):
    response = admin.get("/api/admin/tv-link", params={"replace": replace})
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["url"]).query)["pair"][0]


def activate(client, pair):
    return client.post("/api/tv/activate", json={"pair": pair})


def test_pair_is_one_use_even_when_two_televisions_open_simultaneously(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    with TestClient(app) as admin, TestClient(app) as tv_a, TestClient(app) as tv_b:
        admin_login(admin)
        pair = pair_link(admin)
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(
                pool.map(
                    lambda client: activate(client, pair).status_code, (tv_a, tv_b)
                )
            )
        assert sorted(statuses) == [200, 409]
        assert admin.get("/api/admin/tv").json() == {
            "active": True,
            "owner_role": "global",
            "can_manage": True,
        }
        assert admin.get("/api/admin/tv-link").status_code == 409
        assert tv_a.post("/api/tv/next", params={"token": pair}).status_code == 401


def test_replace_keeps_old_primary_until_new_device_connects(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    with TestClient(app) as admin, TestClient(app) as old, TestClient(app) as new:
        admin_login(admin)
        first = activate(old, pair_link(admin)).json()["token"]
        assert old.get("/api/tv/state", params={"token": first}).status_code == 200
        next_pair = pair_link(admin, replace=True)
        # Issuing a link must not interrupt currently running playback.
        assert old.post("/api/tv/next", params={"token": first}).status_code == 200
        second = activate(new, next_pair).json()["token"]
        assert old.get("/api/tv/state", params={"token": first}).status_code == 401
        assert old.post("/api/tv/next", params={"token": first}).status_code == 401
        assert (
            old.post(
                "/api/tv/finish", params={"token": first}, json={"song_id": 1}
            ).status_code
            == 401
        )
        assert new.get("/api/tv/state", params={"token": second}).status_code == 200
        assert activate(old, next_pair).status_code == 409
        assert admin.post("/api/admin/tv/revoke").status_code == 200
        assert new.get("/api/tv/state", params={"token": second}).status_code == 401
        assert not admin.get("/api/admin/tv").json()["active"]


def test_event_admin_cannot_replace_or_revoke_global_primary(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    with (
        TestClient(app) as global_admin,
        TestClient(app) as event_admin,
        TestClient(app) as tv,
    ):
        admin_login(global_admin)
        create_account(global_admin)
        assert event_login(event_admin).status_code == 200
        master = activate(tv, pair_link(global_admin)).json()["token"]
        state = event_admin.get("/api/admin/tv").json()
        assert (
            state["active"]
            and state["owner_role"] == "global"
            and not state["can_manage"]
        )
        assert (
            event_admin.get("/api/admin/tv-link", params={"replace": True}).status_code
            == 403
        )
        assert event_admin.post("/api/admin/tv/revoke").status_code == 403
        assert tv.get("/api/tv/state", params={"token": master}).status_code == 200
        assert global_admin.post("/api/admin/tv/revoke").status_code == 200
        assert event_admin.get("/api/admin/tv").json()["can_manage"]


def test_global_admin_can_take_over_event_admin_primary(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    with (
        TestClient(app) as global_admin,
        TestClient(app) as event_admin,
        TestClient(app) as a,
        TestClient(app) as b,
    ):
        admin_login(global_admin)
        create_account(global_admin)
        assert event_login(event_admin).status_code == 200
        event_token = activate(a, pair_link(event_admin)).json()["token"]
        pending = pair_link(global_admin, replace=True)
        assert a.get("/api/tv/state", params={"token": event_token}).status_code == 200
        global_token = activate(b, pending).json()["token"]
        assert a.get("/api/tv/state", params={"token": event_token}).status_code == 401
        assert b.get("/api/tv/state", params={"token": global_token}).status_code == 200
        assert event_admin.post("/api/admin/tv/revoke").status_code == 403


def test_reopen_room_restores_primary_and_revocation(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    with TestClient(app) as admin:
        admin_login(admin)
        token = activate(admin, pair_link(admin)).json()["token"]
    reopened = app_at(tmp_path)
    with TestClient(reopened) as admin:
        admin_login(admin)
        assert admin.get("/api/tv/state", params={"token": token}).status_code == 200
        assert admin.post("/api/admin/tv/revoke").status_code == 200
        assert admin.get("/api/tv/state", params={"token": token}).status_code == 401


def test_event_admin_may_revoke_own_primary_without_canceling_global_pending(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    with (
        TestClient(app) as global_admin,
        TestClient(app) as event_admin,
        TestClient(app) as old_tv,
        TestClient(app) as new_tv,
    ):
        admin_login(global_admin)
        create_account(global_admin)
        assert event_login(event_admin).status_code == 200
        old_token = activate(old_tv, pair_link(event_admin)).json()["token"]
        global_pending = pair_link(global_admin, replace=True)
        assert event_admin.post("/api/admin/tv/revoke").status_code == 200
        assert (
            old_tv.get("/api/tv/state", params={"token": old_token}).status_code == 401
        )
        # Global admin's independently issued pending takeover survives.
        assert event_admin.get("/api/admin/tv-link").status_code == 403
        new_token = activate(new_tv, global_pending).json()["token"]
        assert (
            new_tv.get("/api/tv/state", params={"token": new_token}).status_code == 200
        )
