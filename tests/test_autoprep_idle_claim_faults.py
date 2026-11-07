"""AutoPrep idle polling must not commit, and transient claim SQL faults must not kill workers."""

import sqlite3
import threading
import time

import pytest

from vc_ktv.autoprep import AutoPrep
from vc_ktv.core.store import Store


def eventually(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ClaimFaultDb:
    def __init__(self, real_db, fault):
        self.real_db = real_db
        self.fault = fault
        self.failures = 0
        self.rollbacks = 0
        self.commits = 0
        self.next_commit = False

    def execute(self, sql, *args):
        if (
            self.fault == "query"
            and "SELECT * FROM songs WHERE status='waiting'" in sql
            and not self.failures
        ):
            self.failures += 1
            raise sqlite3.OperationalError("injected claim SELECT failure")
        if (
            self.fault == "update"
            and "UPDATE songs SET prep_state='downloading'" in sql
            and not self.failures
        ):
            self.failures += 1
            raise sqlite3.OperationalError("injected claim UPDATE failure")
        if (
            self.fault == "settings"
            and "SELECT name,value FROM room_state" in sql
            and not self.failures
        ):
            self.failures += 1
            raise sqlite3.OperationalError("injected claim settings failure")
        result = self.real_db.execute(sql, *args)
        if (
            self.fault == "commit"
            and "UPDATE songs SET prep_state='downloading'" in sql
        ):
            self.next_commit = True
        return result

    def commit(self):
        self.commits += 1
        if self.fault == "commit" and self.next_commit and not self.failures:
            self.failures += 1
            self.next_commit = False
            raise sqlite3.OperationalError("injected claim COMMIT failure")
        return self.real_db.commit()

    def rollback(self):
        self.rollbacks += 1
        self.next_commit = False
        return self.real_db.rollback()

    def __getattr__(self, name):
        return getattr(self.real_db, name)


@pytest.mark.parametrize("workers", (1, 4))
def test_idle_claim_has_no_sqlite_commit_or_mutation(tmp_path, workers):
    store = Store(tmp_path / "room")
    real_db = store.db

    class NoIdleCommit:
        commits = 0

        def commit(self):
            self.commits += 1
            raise sqlite3.OperationalError("idle read must not COMMIT")

        def __getattr__(self, name):
            return getattr(real_db, name)

    counter = NoIdleCommit()
    store.db = counter
    worker = AutoPrep(store, lambda _: 8, max_workers=workers)
    for _ in range(4):
        assert worker.claim() is None
    assert counter.commits == 0
    assert not real_db.in_transaction


@pytest.mark.parametrize("workers", (1, 4))
@pytest.mark.parametrize("fault", ("query", "update", "commit", "settings"))
def test_transient_claim_sql_failure_rolls_back_and_workers_keep_processing(
    tmp_path, workers, fault
):
    store = Store(tmp_path / "room")
    original_db = store.db
    downloaded = threading.Event()

    def download(bvid, page, directory):
        downloaded.set()
        path = directory / "source.mp4"
        path.write_bytes(b"sample")
        return path

    worker = AutoPrep(store, lambda _: 8, download=download, max_workers=workers)
    worker.start()
    try:
        with store.lock:
            original_db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
            song_id = original_db.execute(
                """INSERT INTO songs(bvid,page,title,uploader,cover,duration,
                   requester,requested_at,status,prep_state)
                   VALUES('BV0000000001',1,'Song','UP','',8,'g',1,'waiting','pending')"""
            ).lastrowid
            original_db.commit()
            wrapped = ClaimFaultDb(original_db, fault)
            store.db = wrapped

        assert eventually(lambda: wrapped.failures == 1), "SQL injection not exercised"
        assert eventually(lambda: downloaded.is_set()), (
            "worker stopped after SQLite failure"
        )
        assert eventually(
            lambda: (
                original_db.execute(
                    "SELECT prep_state FROM songs WHERE id=?", (song_id,)
                ).fetchone()[0]
                == "downloaded"
            )
        ), "task not persisted after retry"
        assert eventually(
            lambda: worker.active_claims == 0 and not worker.busy.is_set()
        )
        with store.lock:
            assert not original_db.in_transaction
            assert wrapped.failures == 1
            assert wrapped.rollbacks == 1
            assert len(worker.workers) == workers
            assert all(thread.is_alive() for thread in worker.workers)
            attempts = original_db.execute(
                "SELECT prep_attempts FROM songs WHERE id=?", (song_id,)
            ).fetchone()[0]
            assert attempts == 1, "failed claim must not consume an attempt"
    finally:
        worker.stop()
        store.db = original_db


def test_failed_claim_rollback_is_fatal_instead_of_retrying_dirty_db(tmp_path):
    store = Store(tmp_path / "room")
    real_db = store.db
    real_db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
    real_db.execute(
        """INSERT INTO songs(bvid,page,title,uploader,cover,duration,
           requester,requested_at,status,prep_state)
           VALUES('BV0000000001',1,'Song','UP','',8,'g',1,'waiting','pending')"""
    )
    real_db.commit()

    class NoRollback:
        def commit(self):
            raise sqlite3.OperationalError("claim commit fault")

        def rollback(self):
            raise sqlite3.OperationalError("claim rollback fault")

        def __getattr__(self, name):
            return getattr(real_db, name)

    store.db = NoRollback()
    worker = AutoPrep(store, lambda _: 8)
    try:
        with pytest.raises(RuntimeError, match="AutoPrep claim rollback failed"):
            worker.claim()
        assert real_db.in_transaction
        assert not worker.busy.is_set()
        assert worker.active_claims == 0
    finally:
        real_db.rollback()
        store.db = real_db
