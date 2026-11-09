"""Task-state boundaries keep queue priority and failed-claim semantics clear."""

import time

import pytest

from tests.test_autoprep_fast_workers import seed_ready
from vc_ktv.core.store import Store
from vc_ktv.processing.worker import AutoPrep


def insert_waiting(store, song_id, *, state="pending", retry_at=0):
    with store.lock:
        store.db.execute(
            """INSERT INTO songs(id,bvid,page,title,uploader,cover,duration,
                requester,requested_at,status,prep_state,prep_retry_at)
                VALUES (?,'BV0000000001',1,'Song','UP','',20,'g',?,'waiting',?,?)""",
            (song_id, song_id, state, retry_at),
        )
        store.db.commit()


def test_provider_subtitles_precede_downloads_on_general_lane(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.commit()
    insert_waiting(store, 1)
    seed_ready(store, 2, "provider")

    worker = AutoPrep(store, lambda _: 20)
    assert worker.claim("general")["id"] == 2
    assert worker.claim("general")["id"] == 1
    assert (
        store.db.execute("SELECT prep_state FROM songs WHERE id=2").fetchone()[0]
        == "aligning"
    )
    assert (
        store.db.execute("SELECT prep_state FROM songs WHERE id=1").fetchone()[0]
        == "downloading"
    )


def test_future_retry_is_skipped_until_eligible(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.commit()
    insert_waiting(store, 1, state="unavailable", retry_at=int(time.time()) + 300)
    insert_waiting(store, 2, state="unavailable", retry_at=int(time.time()) - 1)
    worker = AutoPrep(store, lambda _: 20)
    assert worker.claim()["id"] == 2
    assert worker.claim() is None
    assert (
        store.db.execute("SELECT prep_state FROM songs WHERE id=1").fetchone()[0]
        == "unavailable"
    )


@pytest.mark.parametrize("reason", ["cancelled", "disk_full"])
def test_failure_report_preserves_terminal_status(tmp_path, reason):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.commit()
    insert_waiting(store, 1)
    worker = AutoPrep(store, lambda _: 20)
    claimed = worker.claim()
    with store.lock:
        if reason == "cancelled":
            store.db.execute("UPDATE songs SET status='cancelled' WHERE id=1")
        else:
            store.db.execute(
                "UPDATE songs SET prep_error='本场 200 GiB 磁盘预算已用尽' WHERE id=1"
            )
        store.db.commit()
    original = dict(store.db.execute("SELECT * FROM songs WHERE id=1").fetchone())
    worker._record_failure(claimed, RuntimeError("download failed"), time.monotonic())
    assert (
        dict(store.db.execute("SELECT * FROM songs WHERE id=1").fetchone()) == original
    )
    assert not store.db.in_transaction
