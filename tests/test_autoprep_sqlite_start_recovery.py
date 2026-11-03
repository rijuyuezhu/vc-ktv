"""AutoPrep startup SQLite recovery errors must rollback before serving the event."""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song
from vc_ktv.app import create_app


@pytest.mark.parametrize("worker_count", (1, 4))
@pytest.mark.parametrize("fault", ("first_update", "second_update", "commit"))
def test_sqlite_recovery_error_rolls_back_and_allows_retry(
    tmp_path, monkeypatch, worker_count, fault
):
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", str(worker_count))
    root = tmp_path / "room"
    original = create_app(
        root=root,
        password="test-admin",
        secret=b"sqlite-boot-recovery",
        auto_prepare=False,
    )
    original.state.events.create("Interrupted preparation", "123456")
    prior_store = original.state.events.current_store
    download_id = add_song(
        prior_store, "guest", status="waiting", prep="downloading", time=1
    )
    align_id = add_song(prior_store, "guest", status="ready", prep="aligning", time=2)
    prior_store.db.close()

    restarted = create_app(
        root=root,
        password="test-admin",
        secret=b"sqlite-boot-recovery",
        auto_prepare=True,
    )
    events = restarted.state.events
    store = events.current_store
    worker = restarted.state.autoprep
    real_db = store.db
    downloaded = threading.Event()

    def download(bvid, page, directory):
        downloaded.set()
        file = directory / "source.mp4"
        file.write_bytes(b"fake-video")
        return file

    worker.download = download
    worker.probe = lambda _: 20

    class FailRecoveryDb:
        failures = 0
        rollbacks = 0

        def execute(self, sql, *args):
            is_first = "prep_state='pending'" in sql and "UPDATE songs" in sql
            is_second = "prep_state='downloaded'" in sql and "UPDATE songs" in sql
            if (
                (fault == "first_update" and is_first)
                or (fault == "second_update" and is_second)
            ) and not self.failures:
                self.failures += 1
                raise sqlite3.OperationalError("injected recovery execute failure")
            return real_db.execute(sql, *args)

        def commit(self):
            if fault == "commit" and not self.failures:
                self.failures += 1
                raise sqlite3.OperationalError("injected recovery commit failure")
            return real_db.commit()

        def rollback(self):
            self.rollbacks += 1
            return real_db.rollback()

        def __getattr__(self, name):
            return getattr(real_db, name)

    injected_db = FailRecoveryDb()
    store.db = injected_db
    with TestClient(restarted) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").json()["autoprep_workers"] == 0
        assert client.get("/api/health").json()["auto_prepare"] is True
        assert client.get("/api/event").json()["event"]["id"] == events.active_id
        assert injected_db.failures == 1
        assert injected_db.rollbacks == 1
        assert not real_db.in_transaction
        assert worker.worker is None
        assert worker.workers == []
        assert not worker.stop_signal.is_set()
        assert events.metadata()["status"] == "active"
        assert events.pointer.read_text().strip() == events.active_id
        with store.lock:
            assert (
                real_db.execute(
                    "SELECT prep_state FROM songs WHERE id=?", (download_id,)
                ).fetchone()[0]
                == "downloading"
            )
            assert (
                real_db.execute(
                    "SELECT prep_state FROM songs WHERE id=?", (align_id,)
                ).fetchone()[0]
                == "aligning"
            )

        # Once the temporary SQLite fault clears, both interrupted phases
        # recover normally and the worker processes the pending download.
        store.db = real_db
        worker.start()
        assert downloaded.wait(5), "recovery retry did not resume downloading"
        assert client.get("/api/health").json()["autoprep_workers"] == worker_count
        with store.lock:
            assert (
                real_db.execute(
                    "SELECT prep_state FROM songs WHERE id=?", (align_id,)
                ).fetchone()[0]
                == "downloaded"
            )


def test_unrecoverable_sqlite_rollback_failure_blocks_startup(tmp_path):
    root = tmp_path / "room"
    app = create_app(
        root=root,
        password="test-admin",
        secret=b"sqlite-rollback-failure",
        auto_prepare=False,
    )
    app.state.events.create("Existing", "123456")
    store = app.state.events.current_store
    add_song(store, "guest", status="waiting", prep="downloading")
    store.db.close()
    restarted = create_app(
        root=root,
        password="test-admin",
        secret=b"sqlite-rollback-failure",
        auto_prepare=True,
    )
    store = restarted.state.events.current_store
    real_db = store.db

    class NoRollbackDb:
        def execute(self, sql, *args):
            if "prep_state='downloaded'" in sql and "UPDATE songs" in sql:
                raise sqlite3.OperationalError("recovery update failed")
            return real_db.execute(sql, *args)

        def rollback(self):
            raise sqlite3.OperationalError("rollback also failed")

        def __getattr__(self, name):
            return getattr(real_db, name)

    store.db = NoRollbackDb()
    with pytest.raises(sqlite3.OperationalError, match="rollback also failed"):
        with TestClient(restarted):
            pass
    assert real_db.in_transaction
    assert restarted.state.autoprep.workers == []
    real_db.rollback()
    assert not real_db.in_transaction
    store.db = real_db
    real_db.close()
