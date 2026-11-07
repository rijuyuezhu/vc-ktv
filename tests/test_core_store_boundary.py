"""The moved Store is the same class seen by existing callers."""

from vc_ktv.app import Store, time_now
from vc_ktv.core.store import Store as CoreStore
from vc_ktv.core.store import time_now as core_time_now


def test_store_symbol_and_clock_compatibility(tmp_path):
    assert Store is CoreStore
    assert time_now is core_time_now
    store = CoreStore(tmp_path / "room")
    try:
        assert store.state()["queue"] == []
        assert store.state()["waiting"] == []
    finally:
        store.db.close()
