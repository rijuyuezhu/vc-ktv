"""Guest/admin cancellation commits before stopping a worker and rolls back faults."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_manual_casting_queue import add_song, new_app


class FaultDB:
    def __init__(self, db, fault):
        self.db = db
        self.fault = fault
        self.failures = 0
        self.rollbacks = 0

    def execute(self, sql, *args):
        if (
            self.fault == "update"
            and sql.startswith("UPDATE songs SET status='cancelled'")
            and not self.failures
        ):
            self.failures += 1
            raise sqlite3.OperationalError("injected cancellation UPDATE failure")
        return self.db.execute(sql, *args)

    def commit(self):
        if self.fault == "commit" and not self.failures:
            self.failures += 1
            raise sqlite3.OperationalError("injected cancellation COMMIT failure")
        return self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.db.rollback()

    def __getattr__(self, name):
        return getattr(self.db, name)


@pytest.mark.parametrize("actor", ["guest", "admin"])
@pytest.mark.parametrize("fault", ["update", "commit"])
def test_cancel_fault_does_not_stop_work_before_committed_state(
    tmp_path, monkeypatch, actor, fault
):
    app = new_app(tmp_path)
    with (
        TestClient(app, raise_server_exceptions=False) as guest,
        TestClient(app, raise_server_exceptions=False) as admin,
    ):
        join_and_register(guest, "Alice")
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        store = app.state.events.active()
        guest_id = store.rows("SELECT id FROM users WHERE username='Alice'")[0]["id"]
        song_id = add_song(store, guest_id)
        stops = []

        def stopped(source):
            # Both stop handlers must run only after the cancellation is committed.
            assert not store.db.in_transaction
            assert (
                store.db.execute(
                    "SELECT status FROM songs WHERE id=?", (song_id,)
                ).fetchone()[0]
                == "cancelled"
            )
            stops.append(source)

        monkeypatch.setattr(
            app.state.lifecycle.worker, "cancel_song", lambda _: stopped("worker")
        )
        monkeypatch.setattr(
            app.state.lifecycle.manual, "stop_song", lambda _: stopped("manual")
        )
        client = guest if actor == "guest" else admin
        path = (
            f"/api/songs/{song_id}/cancel"
            if actor == "guest"
            else f"/api/admin/songs/{song_id}/cancel"
        )
        real_db = store.db
        injected = FaultDB(real_db, fault)
        store.db = injected
        try:
            response = client.post(path)
        finally:
            store.db = real_db
        assert response.status_code == 500
        assert injected.failures == injected.rollbacks == 1
        assert not real_db.in_transaction
        assert (
            store.db.execute(
                "SELECT status FROM songs WHERE id=?", (song_id,)
            ).fetchone()[0]
            == "ready"
        )
        assert stops == []

        assert client.post(path).json() == {"ok": True}
        assert stops == ["worker", "manual"]
        assert client.post(path).status_code == 409
        assert stops == ["worker", "manual"]
        assert not real_db.in_transaction
