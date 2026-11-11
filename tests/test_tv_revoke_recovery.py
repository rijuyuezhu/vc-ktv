"""Revoked TV ownership must not strand a playing song."""

import sqlite3
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.test_tv_primary import activate, admin_login, app_at, pair_link


def song_fixture(app):
    with app.state.store.lock:
        db = app.state.store.db
        db.execute("INSERT INTO guests(id,name) VALUES ('alice','Alice')")
        for index in (1, 2):
            db.execute(
                """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,
                   requested_at,status,prep_state,media_file) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"BV{index}",
                    1,
                    f"Song {index}",
                    "Tester",
                    "",
                    120,
                    "alice",
                    int(time.time()) + index,
                    "ready",
                    "done",
                    f"{index}.mp4",
                ),
            )
        db.commit()


def current_status(app, song_id):
    with app.state.store.lock:
        return app.state.store.db.execute(
            "SELECT status,played_at FROM songs WHERE id=?", (song_id,)
        ).fetchone()


def test_revoke_requeues_current_and_leaves_other_pins_and_credits(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    song_fixture(app)
    with TestClient(app) as admin, TestClient(app) as tv:
        admin_login(admin)
        token = activate(tv, pair_link(admin)).json()["token"]
        active = tv.post("/api/tv/next", params={"token": token}).json()["active"]
        assert active["id"] == 1
        assert current_status(app, 1)[0] == "playing"
        with app.state.store.lock:
            app.state.store.db.execute("UPDATE songs SET priority_next=1 WHERE id=2")
            app.state.store.db.commit()
            before = app.state.store.db.execute(
                "SELECT plays,seconds,fair_offset FROM guests WHERE id='alice'"
            ).fetchone()
        result = admin.post("/api/admin/tv/revoke")
        assert result.status_code == 200
        assert result.json()["requeued_song_id"] == 1
        assert tv.get("/api/tv/state", params={"token": token}).status_code == 401
        assert current_status(app, 1)[0] == "ready"
        assert current_status(app, 1)[1] is None
        assert admin.get("/api/admin/state").json()["current"] is None
        assert [s["id"] for s in admin.get("/api/admin/state").json()["queue"]] == [
            2,
            1,
        ]
        with app.state.store.lock:
            after = app.state.store.db.execute(
                "SELECT plays,seconds,fair_offset FROM guests WHERE id='alice'"
            ).fetchone()
        assert after == before
        # The session can change mode immediately; current turn did not lock it.
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "manual"}).status_code
            == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        new_token = activate(tv, pair_link(admin)).json()["token"]
        assert (
            tv.post("/api/tv/next", params={"token": new_token}).json()["active"]["id"]
            == 2
        )


def test_restore_existing_orphan_current_without_invalidating_pairing(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    song_fixture(app)
    with TestClient(app) as admin, TestClient(app) as tv, TestClient(app) as guest:
        admin_login(admin)
        token = activate(tv, pair_link(admin)).json()["token"]
        assert (
            tv.post("/api/tv/next", params={"token": token}).json()["active"]["id"] == 1
        )
        # Simulate a room left behind by an older version of the revoke endpoint.
        with app.state.store.lock:
            app.state.tv_session.write_record("tv_primary", None)
            app.state.store.db.commit()
        pending = pair_link(admin)
        assert guest.post("/api/admin/tv/restore-current").status_code == 401
        response = admin.post("/api/admin/tv/restore-current")
        assert response.status_code == 200
        assert response.json()["requeued_song_id"] == 1
        assert current_status(app, 1)[0] == "ready"
        assert admin.post("/api/admin/tv/restore-current").status_code == 409
        # Restoring a song does not revoke the pending new TV link.
        new_token = activate(tv, pending).json()["token"]
        assert tv.get("/api/tv/state", params={"token": new_token}).status_code == 200
        assert admin.post("/api/admin/tv/restore-current").status_code == 409


def test_restore_rejects_other_modes(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    song_fixture(app)
    with TestClient(app) as admin:
        admin_login(admin)
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "browser"}).status_code
            == 200
        )
        assert admin.post("/api/admin/tv/restore-current").status_code == 409
        assert current_status(app, 1)[0] == "ready"


def test_revoke_failure_rolls_back_both_song_and_tv_owner(tmp_path):
    app = app_at(tmp_path)
    app.state.events.create("Test", "123456")
    song_fixture(app)
    with TestClient(app) as admin, TestClient(app) as tv:
        admin_login(admin)
        token = activate(tv, pair_link(admin)).json()["token"]
        assert tv.post("/api/tv/next", params={"token": token}).status_code == 200
        original = app.state.tv_session.write_record

        def failed_write(name, value):
            if name == "tv_primary" and value is None:
                raise sqlite3.OperationalError("injected TV DB failure")
            return original(name, value)

        with patch.object(
            app.state.tv_session, "write_record", side_effect=failed_write
        ):
            try:
                admin.post("/api/admin/tv/revoke")
            except sqlite3.OperationalError:
                pass  # TestClient re-raises unhandled errors.
        assert current_status(app, 1)[0] == "playing"
        assert tv.get("/api/tv/state", params={"token": token}).status_code == 200
