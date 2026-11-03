"""Store reads must not observe uncommitted writes on a shared SQLite connection."""

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import pytest

from vc_ktv.app import Store


def test_store_rows_blocks_on_uncommitted_worker_transaction(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES ('singer','Initial')")
        store.db.commit()
    started = threading.Event()

    def read():
        started.set()
        return store.rows("SELECT name FROM guests WHERE id='singer'")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.lock:
            store.db.execute("UPDATE guests SET name='Not committed' WHERE id='singer'")
            future = pool.submit(read)
            assert started.wait(2)
            try:
                with pytest.raises(FutureTimeout):
                    future.result(timeout=0.4)
            finally:
                store.db.rollback()
        assert future.result(timeout=3) == [{"name": "Initial"}]
