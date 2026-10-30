"""Per-event administrators are operators, not global history/account owners."""

from fastapi.testclient import TestClient

from tests.test_events import admin_login, make_app


def create_account(client, username="operator", password="event-pass-1"):
    response = client.post(
        "/api/admin/accounts", json={"username": username, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def event_login(client, username="operator", password="event-pass-1"):
    return client.post(
        "/api/admin/login", json={"username": username, "password": password}
    )


def test_event_admin_can_operate_but_cannot_manage_accounts_or_other_events(tmp_path):
    app = make_app(tmp_path)
    with (
        TestClient(app) as owner,
        TestClient(app) as operator,
        TestClient(app) as outsider,
    ):
        admin_login(owner)
        a = owner.post("/api/admin/events", json={"name": "A"}).json()
        account_id = create_account(owner)
        assert account_id
        assert outsider.get("/api/admin/accounts").status_code == 401
        assert event_login(operator).status_code == 200
        own = operator.get("/api/admin/events").json()
        assert own["role"] == "event" and own["username"] == "operator"
        assert own["active"] == a["id"]
        assert [e["id"] for e in own["events"]] == [a["id"]]
        assert owner.get("/api/admin/events").json()["role"] == "global"
        assert operator.get("/api/admin/state").status_code == 200
        assert operator.get("/api/admin/settings").status_code == 200
        assert (
            operator.put(
                "/api/admin/settings",
                json={
                    "queue_policy": "fcfs",
                    "allow_unsubtitled": True,
                    "video_quality": 480,
                },
            ).status_code
            == 200
        )
        assert owner.get("/api/admin/settings").json()["video_quality"] == 480
        assert (
            operator.put(
                "/api/admin/playback-mode", json={"mode": "manual"}
            ).status_code
            == 200
        )
        assert operator.get("/api/admin/played").status_code == 200
        assert operator.get(f"/api/admin/events/{a['id']}/songs.csv").status_code == 200
        assert operator.get("/api/admin/tv-link").status_code == 200
        assert operator.post("/api/admin/next").status_code == 200
        assert operator.post("/api/admin/events", json={"name": "B"}).status_code == 403
        assert operator.get("/api/admin/accounts").status_code == 403
        assert (
            operator.post(
                "/api/admin/accounts", json={"username": "x", "password": "secret12"}
            ).status_code
            == 403
        )
        assert (
            operator.put(
                f"/api/admin/accounts/{account_id}/password",
                json={"password": "another12"},
            ).status_code
            == 403
        )
        assert operator.delete(f"/api/admin/accounts/{account_id}").status_code == 403
        assert operator.get("/api/admin/events/legacy/songs.csv").status_code == 403
        assert (
            operator.get("/api/admin/events/nonexistent/songs.csv").status_code == 403
        )


def test_event_closing_invalidates_operator_cookie_and_tv_link(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as owner, TestClient(app) as operator:
        admin_login(owner)
        a = owner.post("/api/admin/events", json={"name": "A"}).json()
        create_account(owner)
        assert event_login(operator).status_code == 200
        tv_a = operator.get("/api/admin/tv-link").json()["url"].split("token=")[1]
        assert operator.get("/api/tv/state", params={"token": tv_a}).status_code == 200
        # An event operator can end their own show.
        assert operator.post("/api/admin/events/close").status_code == 200
        assert operator.get("/api/admin/state").status_code == 401
        assert operator.get("/api/admin/events").status_code == 401
        assert operator.get("/api/tv/state", params={"token": tv_a}).status_code == 401
        assert event_login(operator).status_code == 401
        b = owner.post("/api/admin/events", json={"name": "B"}).json()
        assert b["id"] != a["id"]
        assert operator.get("/api/admin/settings").status_code == 401
        assert operator.post("/api/admin/next").status_code == 401
        assert operator.get(f"/api/admin/events/{a['id']}/songs.csv").status_code == 401
        assert operator.get(f"/api/admin/events/{b['id']}/songs.csv").status_code == 401
        assert owner.get(f"/api/admin/events/{a['id']}/songs.csv").status_code == 200
        assert owner.get(f"/api/admin/events/{b['id']}/songs.csv").status_code == 200
        assert len(owner.get("/api/admin/events").json()["events"]) == 2
        assert owner.get("/api/admin/accounts").json()["accounts"] == []
        assert owner.get("/api/tv/state", params={"token": tv_a}).status_code == 401
        assert event_login(operator).status_code == 401


def test_password_reset_and_revocation_invalidate_cookie_and_tv_immediately(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as owner, TestClient(app) as first, TestClient(app) as second:
        admin_login(owner)
        owner.post("/api/admin/events", json={"name": "KTV"})
        account_id = create_account(owner)
        listed = owner.get("/api/admin/accounts").json()["accounts"]
        assert listed == [{"id": account_id, "username": "operator"}]
        assert (
            owner.post(
                "/api/admin/accounts",
                json={"username": "OPERATOR", "password": "different1"},
            ).status_code
            == 409
        )
        assert event_login(first).status_code == 200
        tv_old = first.get("/api/admin/tv-link").json()["url"].split("token=")[1]
        assert first.get("/api/tv/state", params={"token": tv_old}).status_code == 200
        assert (
            owner.put(
                f"/api/admin/accounts/{account_id}/password",
                json={"password": "new-password-123"},
            ).status_code
            == 200
        )
        assert first.get("/api/admin/state").status_code == 401
        assert first.get("/api/tv/state", params={"token": tv_old}).status_code == 401
        assert event_login(second).status_code == 401
        assert event_login(second, password="new-password-123").status_code == 200
        tv_new = second.get("/api/admin/tv-link").json()["url"].split("token=")[1]
        assert second.get("/api/tv/state", params={"token": tv_new}).status_code == 200
        assert owner.delete(f"/api/admin/accounts/{account_id}").status_code == 200
        assert second.get("/api/admin/state").status_code == 401
        assert second.get("/api/tv/state", params={"token": tv_new}).status_code == 401
        assert event_login(second, password="new-password-123").status_code == 401
        assert owner.get("/api/admin/accounts").json()["accounts"] == []


def test_legacy_admin_login_and_new_admin_work_after_restart(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as owner:
        admin_login(owner)
        owner.post("/api/admin/events", json={"name": "KTV"})
        create_account(owner, "Alice", "some-pass-123")
    restarted = make_app(tmp_path)
    with TestClient(restarted) as owner, TestClient(restarted) as operator:
        # The old password-only admin login must remain valid.
        admin_login(owner)
        assert owner.get("/api/admin/events").json()["role"] == "global"
        assert event_login(operator, "alice", "some-pass-123").status_code == 200
        assert operator.get("/api/admin/state").status_code == 200
        assert operator.get("/api/admin/events").json()["role"] == "event"


def test_operator_controls_song_and_downloads_video_without_guest_cookie(tmp_path):
    from tests.helpers import join_and_register

    app = make_app(tmp_path)
    with (
        TestClient(app) as owner,
        TestClient(app) as operator,
        TestClient(app) as guest,
    ):
        admin_login(owner)
        owner.post("/api/admin/events", json={"name": "演唱会", "code": "000123"})
        create_account(owner)
        assert event_login(operator).status_code == 200
        join_and_register(guest, "观众", "000123")
        song_id = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        assert (
            operator.post(
                f"/api/admin/songs/{song_id}/media",
                files={"file": ("video.mp4", b"sample-video", "video/mp4")},
            ).status_code
            == 200
        )
        assert (
            operator.put(
                "/api/admin/settings",
                json={"queue_policy": "fair", "allow_unsubtitled": True},
            ).status_code
            == 200
        )
        assert operator.get(f"/api/songs/{song_id}/download").content == b"sample-video"
        assert operator.get("/api/state").status_code == 200
        assert operator.post("/api/admin/next").json()["active"]["id"] == song_id
        assert (
            operator.post("/api/admin/finish", json={"song_id": song_id}).status_code
            == 200
        )
        assert (
            operator.get(
                f"/api/admin/events/{app.state.events.active_id}/songs.csv"
            ).status_code
            == 200
        )


def test_current_event_operator_cannot_fetch_archived_event_csv(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as owner, TestClient(app) as operator:
        admin_login(owner)
        a = owner.post("/api/admin/events", json={"name": "A"}).json()
        owner.post("/api/admin/events/close")
        b = owner.post("/api/admin/events", json={"name": "B"}).json()
        create_account(owner)
        assert event_login(operator).status_code == 200
        assert [
            e["id"] for e in operator.get("/api/admin/events").json()["events"]
        ] == [b["id"]]
        assert operator.get(f"/api/admin/events/{a['id']}/songs.csv").status_code == 403
        assert operator.get(f"/api/admin/events/{b['id']}/songs.csv").status_code == 200
        assert owner.get(f"/api/admin/events/{a['id']}/songs.csv").status_code == 200
