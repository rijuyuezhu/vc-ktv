"""Repeated TV replay requests must keep the previous next-song pin on SQL faults."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.helpers import primary_tv_token
from tests.test_manual_casting_queue import add_song, new_app


class ReplayFault:
    def __init__(self, db, fault):
        self.db = db
        self.fault = fault
        self.failed = False
        self.rollbacks = 0

    def execute(self, sql, *args):
        if not self.failed and (
            (self.fault == "clear" and "UPDATE songs SET priority_next=0" in sql)
            or (self.fault == "set" and "UPDATE songs SET priority_next=1" in sql)
        ):
            self.failed = True
            raise sqlite3.OperationalError("injected replay pin write failure")
        return self.db.execute(sql, *args)

    def commit(self):
        if self.fault == "commit" and not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("injected replay pin COMMIT failure")
        return self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.db.rollback()

    def __getattr__(self, key):
        return getattr(self.db, key)


@pytest.mark.parametrize("fault", ("clear", "set", "commit"))
def test_repeated_tv_replay_pin_failure_preserves_previous_next_song(tmp_path, fault):
    app = new_app(tmp_path)
    store = app.state.store
    first = add_song(store, "A", media="first.mp4", time=1)
    other = add_song(store, "B", media="other.mp4", time=2)
    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        token = primary_tv_token(admin)
        assert (
            admin.post("/api/tv/next", params={"token": token}).json()["active"]["id"]
            == first
        )
        path = "/api/tv/replay-next"
        body = {"song_id": first}
        first_replay = admin.post(path, params={"token": token}, json=body)
        assert first_replay.status_code == 200, first_replay.text
        replay_id = first_replay.json()["id"]
        assert admin.post(f"/api/admin/songs/{other}/pin-next").status_code == 200

        real_db = store.db
        faulty = ReplayFault(real_db, fault)
        store.db = faulty
        try:
            failed = admin.post(path, params={"token": token}, json=body)
        finally:
            store.db = real_db
        assert failed.status_code == 500
        assert faulty.failed
        assert faulty.rollbacks == 1
        assert not real_db.in_transaction
        assert store.rows("SELECT id FROM songs WHERE priority_next=1 ORDER BY id") == [
            {"id": other}
        ]
        assert admin.get("/api/state").json()["queue"][0]["id"] == other
        retried = admin.post(path, params={"token": token}, json=body)
        assert retried.status_code == 200, retried.text
        assert retried.json()["id"] == replay_id
        assert store.rows("SELECT id FROM songs WHERE priority_next=1 ORDER BY id") == [
            {"id": replay_id}
        ]
        assert store.rows(
            "SELECT id FROM songs WHERE replay_of=? AND status='ready'", (first,)
        ) == [{"id": replay_id}]
