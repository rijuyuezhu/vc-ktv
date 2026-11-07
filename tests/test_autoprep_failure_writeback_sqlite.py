"""A transient SQLite fault while recording media failure must not kill AutoPrep."""

import sqlite3
import threading
import time

import pytest

from tests.test_autoprep_concurrency import seed_downloads
from tests.test_stage_durations import ready_song
from vc_ktv.autoprep import AutoPrep
from vc_ktv.core.store import Store


def eventually(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("workers", (1, 4))
@pytest.mark.parametrize("fault", ("update", "commit"))
@pytest.mark.parametrize("phase", ("download", "subtitle"))
def test_failure_writeback_sqlite_fault_rolls_back_and_retries(
    tmp_path, workers, fault, phase
):
    if phase == "download":
        store = Store(tmp_path / "room")
        seed_downloads(store, 1)
        song_id = 1
    else:
        store, _, song_id = ready_song(tmp_path)

    real_db = store.db
    expected = "unavailable" if phase == "download" else "original"
    target_sql = (
        "UPDATE songs SET prep_state='unavailable'"
        if phase == "download"
        else "UPDATE songs SET prep_state='original'"
    )
    fault_reached = threading.Event()

    class FaultDb:
        failures = 0
        rollbacks = 0
        pending_commit = False

        def execute(self, sql, *args):
            if target_sql in sql and not self.failures:
                if fault == "update":
                    self.failures += 1
                    fault_reached.set()
                    raise sqlite3.OperationalError(
                        "injected failure status UPDATE fault"
                    )
                self.pending_commit = True
            return real_db.execute(sql, *args)

        def commit(self):
            if self.pending_commit and not self.failures:
                self.pending_commit = False
                self.failures += 1
                fault_reached.set()
                raise sqlite3.OperationalError("injected failure status COMMIT fault")
            return real_db.commit()

        def rollback(self):
            self.rollbacks += 1
            return real_db.rollback()

        def __getattr__(self, name):
            return getattr(real_db, name)

    wrapped = FaultDb()
    store.db = wrapped
    failed_downloads = []

    def fail_download(*args, **kwargs):
        failed_downloads.append(1)
        raise RuntimeError("simulated media preparation failure")

    worker = AutoPrep(
        store, probe=lambda p: 8, download=fail_download, max_workers=workers
    )
    if phase == "subtitle":
        worker.prepare_subtitles = lambda _: (_ for _ in ()).throw(
            RuntimeError("simulated unexpected subtitle failure")
        )
    worker.start()
    try:
        assert fault_reached.wait(4), "test did not inject the DB error"
        assert eventually(
            lambda: (
                real_db.execute(
                    "SELECT prep_state FROM songs WHERE id=?", (song_id,)
                ).fetchone()[0]
                == expected
            )
        ), "failed job never received its intended error status after DB recovered"
        assert eventually(
            lambda: worker.active_claims == 0 and not worker.busy.is_set()
        )
        with store.lock:
            assert wrapped.failures == 1
            assert wrapped.rollbacks == 1
            assert not real_db.in_transaction
            assert len(worker.workers) == workers
            assert all(t.is_alive() for t in worker.workers)
            status = real_db.execute(
                "SELECT prep_state,prep_error FROM songs WHERE id=?", (song_id,)
            ).fetchone()
            assert status["prep_state"] == expected
            assert status["prep_error"]
            if phase == "download":
                assert len(failed_downloads) == 1
                assert (
                    real_db.execute(
                        "SELECT prep_attempts FROM songs WHERE id=?", (song_id,)
                    ).fetchone()[0]
                    == 1
                )
    finally:
        worker.stop()
        if real_db.in_transaction:
            real_db.rollback()
        store.db = real_db


@pytest.mark.parametrize("phase", ("download", "subtitle"))
def test_persistent_failure_writeback_error_can_stop_worker_cleanly(tmp_path, phase):
    if phase == "download":
        store = Store(tmp_path / "room")
        seed_downloads(store, 1)
        expected = "downloading"
        target_sql = "UPDATE songs SET prep_state='unavailable'"
    else:
        store, _, _ = ready_song(tmp_path)
        expected = "aligning"
        target_sql = "UPDATE songs SET prep_state='original'"
    real_db = store.db
    entered = threading.Event()

    class AlwaysBroken:
        def execute(self, sql, *args):
            if target_sql in sql:
                entered.set()
                raise sqlite3.OperationalError("persistent writeback failure")
            return real_db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(real_db, name)

    store.db = AlwaysBroken()

    def fail_download(*args, **kwargs):
        raise RuntimeError("download error")

    worker = AutoPrep(store, lambda p: 8, download=fail_download)
    if phase == "subtitle":
        worker.prepare_subtitles = lambda _: (_ for _ in ()).throw(
            RuntimeError("render error")
        )
    worker.start()
    try:
        assert entered.wait(4)
        assert worker.worker and worker.worker.is_alive()
        assert worker.busy.is_set()
        assert worker.stop()
        assert not worker.worker.is_alive()
        assert worker.active_claims == 0
        assert not worker.busy.is_set()
        assert real_db.execute("SELECT prep_state FROM songs").fetchone()[0] == expected
        assert not real_db.in_transaction
    finally:
        worker.stop()
        store.db = real_db


def test_unrecoverable_failure_writeback_rollback_does_not_retry(tmp_path):
    store = Store(tmp_path / "room")
    seed_downloads(store, 1)
    worker = AutoPrep(store, lambda p: 8)
    row = worker.claim()
    real_db = store.db

    class CannotRollback:
        def commit(self):
            raise sqlite3.OperationalError("simulated writeback COMMIT fault")

        def rollback(self):
            raise sqlite3.OperationalError("simulated rollback fault")

        def __getattr__(self, name):
            return getattr(real_db, name)

    store.db = CannotRollback()
    try:
        with pytest.raises(
            RuntimeError, match="AutoPrep failure writeback rollback failed"
        ):
            worker._record_failure(
                row, RuntimeError("download failed"), time.monotonic()
            )
        assert real_db.in_transaction
        assert (
            real_db.execute("SELECT prep_state FROM songs").fetchone()[0]
            == "unavailable"
        )
    finally:
        real_db.rollback()
        store.db = real_db
