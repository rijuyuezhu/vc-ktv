"""HTTP and AutoPrep must not commit one another's SQLite transactions."""

import threading

from vc_ktv.core.store import Store
from vc_ktv.events import CurrentStore, Events


def test_http_and_worker_do_not_share_unlocked_sqlite_transaction(tmp_path):
    events = Events(tmp_path / "ktv", Store)
    events.create("Test", "123456")
    http_store = CurrentStore(events)
    worker_store = events.active()
    attempting = threading.Event()
    acquired = threading.Event()

    def worker_write():
        attempting.set()
        with worker_store.lock:
            acquired.set()
            worker_store.db.execute(
                "INSERT INTO guests(id,name) VALUES ('worker','Worker')"
            )
            worker_store.db.commit()

    thread = threading.Thread(target=worker_write)
    with http_store.lock:
        http_store.db.execute(
            "INSERT INTO guests(id,name) VALUES ('http','Not committed')"
        )
        thread.start()
        assert attempting.wait(2), "Worker did not attempt its transaction"
        # The worker MUST wait until the HTTP transaction is rolled back.
        overlapped = acquired.wait(0.5)
        http_store.db.rollback()
    thread.join(timeout=3)
    assert not thread.is_alive(), "Worker did not complete after HTTP lock released"
    assert not overlapped, "Worker entered while HTTP transaction was in flight"
    assert worker_store.rows("SELECT id FROM guests ORDER BY id") == [{"id": "worker"}]
