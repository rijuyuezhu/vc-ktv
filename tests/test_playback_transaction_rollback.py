"""Failed playback transitions must not leak unfinished SQLite transactions."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.helpers import use_browser_playback
from tests.test_fair_capped_two import ready
from vc_ktv.app import create_app


class FailOnceSql:
    def __init__(self, real_db, fail_sql=None):
        self.real_db = real_db
        self.fail_sql = fail_sql
        self.failures = 0
        self.rollbacks = 0

    def execute(self, sql, *args):
        if self.fail_sql and self.fail_sql in sql and not self.failures:
            self.failures += 1
            raise sqlite3.OperationalError("injected playback UPDATE failure")
        return self.real_db.execute(sql, *args)

    def commit(self):
        if self.fail_sql is None and not self.failures:
            self.failures += 1
            raise sqlite3.OperationalError("injected playback commit failure")
        return self.real_db.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.real_db.rollback()

    def __getattr__(self, name):
        return getattr(self.real_db, name)


def start_room(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"playback-transactions",
        auto_prepare=False,
    )
    app.state.events.create("Playing", "123456")
    use_browser_playback(app)
    return app, app.state.events.current_store


def login(client):
    assert (
        client.post("/api/admin/login", json={"password": "test-admin"}).status_code
        == 200
    )


@pytest.mark.parametrize("worker_total", (1, 4))
@pytest.mark.parametrize("fault", ("commit", "credit", "claim"))
def test_failed_claim_rolls_back_virtual_credit_and_song_state(
    tmp_path, monkeypatch, worker_total, fault
):
    # Worker count is intentionally irrelevant: the scheduler is Store-owned.
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", str(worker_total))
    app, store = start_room(tmp_path)
    with store.lock:
        store.db.executemany(
            "INSERT INTO guests(id,name,plays) VALUES (?,?,?)",
            (("a", "A", 10), ("b", "B", 0)),
        )
        store.db.commit()
        ready(store, "A", 1)
        ready(store, "B", 2)
        assert store.fair_queue()[0]["nickname"] == "B"

    with TestClient(app, raise_server_exceptions=False) as admin:
        login(admin)
        real_db = store.db
        fail_sql = {
            "commit": None,
            "credit": "UPDATE guests SET fair_offset=fair_offset+?",
            "claim": "UPDATE songs SET status='playing'",
        }[fault]
        faildb = FailOnceSql(real_db, fail_sql)
        store.db = faildb
        try:
            failed = admin.post("/api/admin/next")
        finally:
            store.db = real_db

        assert failed.status_code == 500
        assert faildb.failures == 1
        assert faildb.rollbacks == 1
        assert not real_db.in_transaction
        assert store.current() is None
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='B'"
            ).fetchone()[0]
            == 0
        )
        assert [s["nickname"] for s in store.fair_queue()] == ["B", "B", "A"]

        retried = admin.post("/api/admin/next")
        assert retried.status_code == 200, retried.text
        assert retried.json()["active"]["nickname"] == "B"
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='B'"
            ).fetchone()[0]
            == 8
        )


@pytest.mark.parametrize("reason", ("completed", "skip"))
@pytest.mark.parametrize("fault", ("commit", "song", "guest", "last"))
def test_failed_finish_rolls_back_song_and_real_virtual_play_counts(
    tmp_path, reason, fault
):
    app, store = start_room(tmp_path)
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('a','A')")
        store.db.commit()
        ready(store, "A", 1)
    with TestClient(app, raise_server_exceptions=False) as admin:
        login(admin)
        claimed = admin.post("/api/admin/next")
        assert claimed.status_code == 200
        song_id = claimed.json()["active"]["id"]
        real_db = store.db
        fail_sql = {
            "commit": None,
            "song": "UPDATE songs SET status=?",
            "guest": (
                "UPDATE guests SET fair_offset=fair_offset+1"
                if reason == "skip"
                else "UPDATE guests SET plays=plays+1"
            ),
            "last": "INSERT INTO room_state(name,value) VALUES('last_requester',?)",
        }[fault]
        faildb = FailOnceSql(real_db, fail_sql)
        store.db = faildb
        try:
            failed = admin.post(
                "/api/admin/finish", json={"song_id": song_id, "reason": reason}
            )
        finally:
            store.db = real_db

        assert failed.status_code == 500
        assert faildb.failures == 1
        assert faildb.rollbacks == 1
        assert not real_db.in_transaction
        assert store.current()["id"] == song_id
        guest = store.db.execute(
            "SELECT plays,fair_offset,seconds FROM guests WHERE id='a'"
        ).fetchone()
        assert tuple(guest) == (0, 0, 0)
        assert not store.db.execute(
            "SELECT 1 FROM room_state WHERE name='last_requester'"
        ).fetchone()

        retried = admin.post(
            "/api/admin/finish", json={"song_id": song_id, "reason": reason}
        )
        assert retried.status_code == 200, retried.text
        assert store.current() is None
        guest = store.db.execute(
            "SELECT plays,fair_offset FROM guests WHERE id='a'"
        ).fetchone()
        assert tuple(guest) == ((0, 1) if reason == "skip" else (1, 0))
