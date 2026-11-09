"""HTTP and AutoPrep must not commit one another's SQLite transactions."""

import threading

from vc_ktv.core.events import CurrentStore, Events
from vc_ktv.core.store import Store


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


def test_worker_store_wait_never_holds_event_admission_lock(tmp_path):
    """A Store mutex held by AutoPrep must not stall unrelated HTTP admission."""
    events = Events(tmp_path / "ktv-admission", Store)
    events.create("Test", "123456")
    worker_store = events.active()
    waited = threading.Event()
    release = threading.Event()
    has_store_lock = threading.Event()
    contender_done = threading.Event()

    class ProbeLock:
        def __init__(self, original):
            self.original = original

        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == "http-contender":
                waited.set()
            return self.original.acquire(*args, **kwargs)

        def release(self):
            self.original.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_):
            self.release()

    worker_store.lock = ProbeLock(worker_store.lock)

    def worker():
        with worker_store.lock:
            has_store_lock.set()
            assert release.wait(timeout=2)

    def stalled_http():
        with CurrentStore(events).lock:
            pass
        contender_done.set()

    worker_thread = threading.Thread(target=worker, name="worker")
    http_thread = threading.Thread(target=stalled_http, name="http-contender")
    worker_thread.start()
    try:
        assert has_store_lock.wait(1)
        http_thread.start()
        assert waited.wait(1)
        # A second request should be admitted while the first is still
        # waiting for the worker's Store mutex. This was 350+ ms before fix.
        assert events.lock.acquire(timeout=0.1), "Store wait monopolized Events.lock"
        events.lock.release()
        assert events.begin_request()
        events.end_request()
        assert events.inflight == 1, "Store operation must pin event during lock wait"
        assert not contender_done.is_set()
    finally:
        release.set()
        worker_thread.join(timeout=2)
        if http_thread.ident is not None:
            http_thread.join(timeout=2)
    assert not worker_thread.is_alive()
    assert not http_thread.is_alive()
    assert contender_done.is_set()


def test_event_close_waits_for_current_store_transaction_pin(tmp_path):
    """Releasing Events.lock early must not close SQLite under a waiting writer."""
    import time

    events = Events(tmp_path / "ktv-close-pin", Store)
    events.create("Test", "123456")
    store = events.active()
    locked, release, entered, completed, closed = (threading.Event() for _ in range(5))

    def worker():
        with store.lock:
            locked.set()
            assert release.wait(3)

    def waiting_http():
        with CurrentStore(events).lock:
            entered.set()
            assert store.db.execute("SELECT COUNT(*) FROM guests").fetchone()[0] == 0
        completed.set()

    def closing():
        events.close()
        closed.set()

    worker_thread = threading.Thread(target=worker)
    waiter_thread = threading.Thread(target=waiting_http)
    closer_thread = threading.Thread(target=closing)
    worker_thread.start()
    try:
        assert locked.wait(2)
        waiter_thread.start()
        deadline = time.monotonic() + 1
        while events.inflight == 0 and time.monotonic() < deadline:
            time.sleep(0.002)
        assert events.inflight == 1
        closer_thread.start()
        deadline = time.monotonic() + 1
        while not events.transitioning and time.monotonic() < deadline:
            time.sleep(0.002)
        assert events.transitioning
        assert not closed.is_set(), "session closed with a pinned Store transaction"
    finally:
        release.set()
        worker_thread.join(timeout=3)
        waiter_thread.join(timeout=3)
        if closer_thread.ident is not None:
            closer_thread.join(timeout=3)
    assert completed.is_set()
    assert closed.is_set()
    assert events.inflight == 0
    assert events.active_id is None
