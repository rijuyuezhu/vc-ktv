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


def test_core_title_parser_has_no_worker_dependency():
    from vc_ktv.autoprep import title_key as legacy_title_key
    from vc_ktv.core.titles import title_key as core_title_key

    assert legacy_title_key is core_title_key
    assert core_title_key("【虚拟歌手】示例曲目") == "示例曲目"
