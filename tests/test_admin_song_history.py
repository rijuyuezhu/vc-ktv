"""Current-event song records are shared by guests and both administrator roles."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_event_admins import create_account, event_login
from tests.test_events import admin_login, make_app


def test_current_song_records_accept_administrators_without_guest_login(tmp_path):
    app = make_app(tmp_path)
    with (
        TestClient(app) as owner,
        TestClient(app) as operator,
        TestClient(app) as guest,
        TestClient(app) as outsider,
    ):
        assert outsider.get("/api/songs/history").status_code == 401
        admin_login(owner)
        assert owner.get("/api/songs/history").json() == {
            "songs": [],
            "archived": False,
        }
        event = owner.post(
            "/api/admin/events", json={"name": "A", "code": "123456"}
        ).json()
        account_id = create_account(owner)
        assert event_login(operator).status_code == 200
        join_and_register(guest, "Singer")
        store = app.state.events.active()
        with store.lock:
            uid = store.db.execute("SELECT id FROM guests LIMIT 1").fetchone()[0]
            for song_id, status in [
                (1, "played"),
                (2, "skipped"),
                (3, "ready"),
                (4, "cancelled"),
            ]:
                store.db.execute(
                    """INSERT INTO songs (id,bvid,page,title,uploader,cover,duration,
                       requester,requested_at,status,prep_state,played_at)
                       VALUES (?, 'BV0000000001', 1, ?, 'UP', '', 60, ?, ?, ?, 'done', ?)""",
                    (
                        song_id,
                        f"歌{song_id}",
                        uid,
                        song_id,
                        status,
                        song_id if status in {"played", "skipped"} else None,
                    ),
                )
            store.db.commit()

        guest_data = guest.get("/api/songs/history").json()
        assert guest_data["archived"] is False
        assert [row["id"] for row in guest_data["songs"]] == [1, 2, 3, 4]
        assert owner.get("/api/songs/history?admin_view=true").json() == guest_data
        assert operator.get("/api/songs/history?admin_view=true").json() == guest_data
        assert outsider.get("/api/songs/history").status_code == 401
        assert outsider.get("/api/songs/history?admin_view=true").status_code == 401
        assert guest.get("/api/songs/history?admin_view=true").status_code == 401

        # Reset immediately revokes the old event-admin cookie.
        assert (
            owner.put(
                f"/api/admin/accounts/{account_id}/password",
                json={"password": "fresh-event-pass"},
            ).status_code
            == 200
        )
        assert operator.get("/api/songs/history?admin_view=true").status_code == 401
        assert (
            operator.post(
                "/api/admin/login",
                json={"username": "operator", "password": "fresh-event-pass"},
            ).status_code
            == 200
        )
        assert operator.get("/api/songs/history?admin_view=true").json() == guest_data

        assert owner.post("/api/admin/events/close").status_code == 200
        assert operator.get("/api/songs/history?admin_view=true").status_code == 401
        assert owner.get("/api/songs/history?admin_view=true").json() == {
            "songs": [],
            "archived": False,
        }
        assert guest.get("/api/songs/history").status_code == 401
        assert (
            owner.put(
                f"/api/admin/events/{event['id']}/visibility?public=true"
            ).status_code
            == 200
        )
        archived = guest.get(f"/api/events/{event['id']}/songs").json()
        assert [row["id"] for row in archived["songs"]] == [1, 2, 3, 4]
        # A global administrator can also browse the same archived event.
        owner.cookies.set("vc_guest", guest.cookies.get("vc_guest"))
        assert owner.get(f"/api/events/{event['id']}/songs").json() == archived
        assert owner.get("/api/songs/history?admin_view=true").json() == {
            "songs": [],
            "archived": False,
        }

        # An old event-admin cookie cannot read the next event's song records.
        assert owner.post("/api/admin/events", json={"name": "B"}).status_code == 200
        assert operator.get("/api/songs/history?admin_view=true").status_code == 401
        assert owner.get("/api/songs/history?admin_view=true").json() == {
            "songs": [],
            "archived": False,
        }
        assert event["id"] != app.state.events.active_id
