"""Administrative queue state changes are atomic, including rejected cancellation."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_manual_casting_queue import add_song, new_app


class FaultSql:
    def __init__(self, db, fault):
        self.db = db
        self.fault = fault
        self.failures = 0
        self.rollbacks = 0

    def execute(self, sql, *args):
        if not self.failures and (
            (self.fault == "pin_clear" and "UPDATE songs SET priority_next=0" in sql)
            or (self.fault == "pin_set" and "UPDATE songs SET priority_next=1" in sql)
        ):
            self.failures += 1
            raise sqlite3.OperationalError("injected admin queue UPDATE failure")
        return self.db.execute(sql, *args)

    def executemany(self, sql, items):
        if self.fault == "settings_partial" and not self.failures:
            values = list(items)
            assert len(values) >= 2
            self.db.execute(sql, values[0])
            self.failures += 1
            raise sqlite3.OperationalError("injected settings mid-batch failure")
        return self.db.executemany(sql, items)

    def commit(self):
        if self.fault == "commit" and not self.failures:
            self.failures += 1
            raise sqlite3.OperationalError("injected admin queue COMMIT failure")
        return self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.db.rollback()

    def __getattr__(self, key):
        return getattr(self.db, key)


def login(admin):
    assert (
        admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
        == 200
    )


@pytest.mark.parametrize("fault", ("pin_clear", "pin_set", "commit"))
def test_pin_next_failed_transaction_keeps_existing_pin(tmp_path, fault):
    app = new_app(tmp_path)
    store = app.state.store
    previous = add_song(store, "singer", media="previous.mp4", time=1)
    chosen = add_song(store, "singer", media="chosen.mp4", time=2)
    with TestClient(app, raise_server_exceptions=False) as admin:
        login(admin)
        assert admin.post(f"/api/admin/songs/{previous}/pin-next").status_code == 200
        real_db = store.db
        faulty = FaultSql(real_db, fault)
        store.db = faulty
        try:
            failed = admin.post(f"/api/admin/songs/{chosen}/pin-next")
        finally:
            store.db = real_db
        assert failed.status_code == 500
        assert faulty.failures == 1
        assert faulty.rollbacks == 1
        assert not real_db.in_transaction
        assert store.rows(
            "SELECT id,priority_next FROM songs WHERE priority_next=1"
        ) == [{"id": previous, "priority_next": 1}]
        assert admin.get("/api/state").json()["queue"][0]["id"] == previous
        assert admin.post(f"/api/admin/songs/{chosen}/pin-next").status_code == 200
        assert store.rows(
            "SELECT id,priority_next FROM songs WHERE priority_next=1"
        ) == [{"id": chosen, "priority_next": 1}]


@pytest.mark.parametrize("fault", ("settings_partial", "commit"))
def test_failed_settings_update_does_not_partially_change_options(tmp_path, fault):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app, raise_server_exceptions=False) as admin:
        login(admin)
        before = store.rows("SELECT name,value FROM room_state ORDER BY name")
        prior_settings = admin.get("/api/admin/settings").json()
        # save_settings is a Store method bound to the active backing store;
        # CurrentStore.db assignment would only shadow the forwarding proxy.
        backing = app.state.events.active()
        real_db = backing.db
        faulty = FaultSql(real_db, fault)
        backing.db = faulty
        body = {
            "queue_policy": "fcfs",
            "allow_unsubtitled": True,
            "orders_paused": True,
            "video_quality": 480,
        }
        try:
            failed = admin.put("/api/admin/settings", json=body)
        finally:
            backing.db = real_db
        assert failed.status_code == 500
        assert faulty.failures == 1
        assert faulty.rollbacks == 1
        assert not real_db.in_transaction
        assert store.rows("SELECT name,value FROM room_state ORDER BY name") == before
        assert admin.get("/api/admin/settings").json() == prior_settings
        updated = admin.put("/api/admin/settings", json=body)
        assert updated.status_code == 200, updated.text
        new_settings = admin.get("/api/admin/settings").json()
        assert new_settings["queue_policy"] == "fcfs"
        assert new_settings["orders_paused"] is True


@pytest.mark.parametrize("actor", ("guest", "admin"))
def test_failed_cancel_commit_does_not_remove_song(tmp_path, actor):
    app = new_app(tmp_path)
    store = app.state.store
    with (
        TestClient(app, raise_server_exceptions=False) as admin,
        TestClient(app, raise_server_exceptions=False) as guest,
    ):
        login(admin)
        join_and_register(guest, "Queue owner")
        owner = store.db.execute(
            "SELECT id FROM users WHERE username='Queue owner'"
        ).fetchone()[0]
        song_id = add_song(store, owner, media="owned.mp4")
        client = admin if actor == "admin" else guest
        route = (
            f"/api/admin/songs/{song_id}/cancel"
            if actor == "admin"
            else f"/api/songs/{song_id}/cancel"
        )
        real_db = store.db
        faulty = FaultSql(real_db, "commit")
        store.db = faulty
        try:
            failed = client.post(route)
        finally:
            store.db = real_db
        assert failed.status_code == 500
        assert faulty.failures == 1
        assert faulty.rollbacks == 1
        assert not real_db.in_transaction
        assert store.rows("SELECT id,status FROM songs WHERE id=?", (song_id,)) == [
            {"id": song_id, "status": "ready"}
        ]
        assert client.post(route).status_code == 200
        assert store.rows("SELECT status FROM songs WHERE id=?", (song_id,)) == [
            {"status": "cancelled"}
        ]


def test_rejected_guest_cancel_does_not_leak_transaction(tmp_path):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app) as guest:
        join_and_register(guest, "Queue owner")
        owner = store.db.execute(
            "SELECT id FROM users WHERE username='Queue owner'"
        ).fetchone()[0]
        song_id = add_song(store, owner, media="played.mp4", status="played")
        response = guest.post(f"/api/songs/{song_id}/cancel")
        assert response.status_code == 409
        assert not store.db.in_transaction
        assert store.rows("SELECT status FROM songs WHERE id=?", (song_id,)) == [
            {"status": "played"}
        ]


def test_failed_playback_mode_commit_does_not_leak_change(tmp_path):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app, raise_server_exceptions=False) as admin:
        login(admin)
        before = store.playback_mode()
        mode = "manual" if before != "manual" else "browser"
        real_db = store.db
        faulty = FaultSql(real_db, "commit")
        store.db = faulty
        try:
            failed = admin.put("/api/admin/playback-mode", json={"mode": mode})
        finally:
            store.db = real_db
        assert failed.status_code == 500
        assert faulty.failures == 1
        assert faulty.rollbacks == 1
        assert not real_db.in_transaction
        assert store.playback_mode() == before
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": mode}).status_code
            == 200
        )
        assert store.playback_mode() == mode
