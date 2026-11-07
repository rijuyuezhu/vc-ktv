"""Event history routes preserve permissions and release-media CSV reuse."""

from fastapi.testclient import TestClient

from tests.test_event_admins import create_account, event_login
from tests.test_events import admin_login, make_app


def test_event_history_routes_are_scoped_and_archive_csv_reused(tmp_path):
    app = make_app(tmp_path / "ktv")
    routes = app.openapi()["paths"]
    assert {
        "/api/admin/events",
        "/api/admin/events/{event_id}/songs",
        "/api/admin/events/{event_id}/songs.csv",
    } <= routes.keys()

    with (
        TestClient(app) as owner,
        TestClient(app) as operator,
        TestClient(app) as guest,
    ):
        admin_login(owner)
        first = owner.post("/api/admin/events", json={"name": "First"}).json()
        create_account(owner)
        assert event_login(operator).status_code == 200
        assert guest.get("/api/admin/events").status_code == 401
        assert (
            guest.get(f"/api/admin/events/{first['id']}/songs.csv").status_code == 401
        )
        own = operator.get("/api/admin/events").json()
        assert own["active"] == first["id"]
        assert [event["id"] for event in own["events"]] == [first["id"]]
        assert operator.get(f"/api/admin/events/{first['id']}/songs").json() == {
            "songs": []
        }
        assert owner.get(
            f"/api/admin/events/{first['id']}/songs.csv"
        ).content.startswith(b"\xef\xbb\xbf")
        assert owner.post("/api/admin/events/close").status_code == 200
        assert (
            owner.post("/api/admin/events", json={"name": "Second"}).status_code == 200
        )
        assert (
            operator.get(f"/api/admin/events/{first['id']}/songs.csv").status_code
            == 401
        )
        previous_csv = owner.get(f"/api/admin/events/{first['id']}/songs.csv").content
        assert (
            owner.post(f"/api/admin/events/{first['id']}/release-media").status_code
            == 200
        )
        stored_csv = (
            tmp_path / "ktv" / "sessions" / first["id"] / "songs.csv"
        ).read_bytes()
        assert stored_csv == previous_csv
