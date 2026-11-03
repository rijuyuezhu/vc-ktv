"""SQLite Store is the authority for playback turns and room settings."""

from tests.test_fair_capped_two import ready
from vc_ktv.app import Store


def test_store_claim_and_settle_preserves_fair_turn_and_is_idempotent(tmp_path):
    store = Store(tmp_path)
    for user in ("A", "B"):
        store.db.execute("INSERT INTO guests(id,name) VALUES (?,?)", (user, user))
        ready(store, user, 2)

    with store.lock:
        expected = store.fair_queue()[0]["id"]
        store.claim_next()
        current = store.current()
        assert current["id"] == expected
        # Repeated next cannot claim another song while one is playing.
        store.claim_next()
        assert store.current()["id"] == expected
        assert len(store.fair_queue()) == 3

        # Skip consumes virtual credit, not an actual performance.
        assert store.finish_current(expected, skipped=True) == 3
        assert store.rows(
            "SELECT plays,seconds,fair_offset FROM guests WHERE id='A'"
        ) == [{"plays": 0, "seconds": 0, "fair_offset": 1}]
        assert store.current() is None

        assert store.finish_current(expected) is None  # Already settled.

        store.claim_next()
        assert store.current()["requester"] == "B"
        second = store.current()["id"]
        assert store.finish_current(second) == 2
        assert store.rows(
            "SELECT plays,seconds,fair_offset FROM guests WHERE id='B'"
        ) == [{"plays": 1, "seconds": 90, "fair_offset": 0}]


def test_store_priority_turn_does_not_charge_real_or_virtual_counts(tmp_path):
    store = Store(tmp_path)
    for user in ("A", "B"):
        store.db.execute("INSERT INTO guests(id,name) VALUES (?,?)", (user, user))
        ready(store, user, 1)
    with store.lock:
        selected = store.rows("SELECT id FROM songs WHERE requester='B'")[0]["id"]
        store.db.execute("UPDATE songs SET priority_next=1 WHERE id=?", (selected,))
        store.db.commit()
        before = store.rows(
            "SELECT id,plays,seconds,fair_offset FROM guests ORDER BY id"
        )
        assert store.fair_queue()[0]["id"] == selected
        store.claim_next()
        assert store.current()["fair_exempt"] == 1
        assert store.current()["priority_next"] == 0
        assert store.finish_current(selected) == 1
        assert (
            store.rows("SELECT id,plays,seconds,fair_offset FROM guests ORDER BY id")
            == before
        )
        assert (
            store.rows("SELECT value FROM room_state WHERE name='last_requester'") == []
        )


def test_optional_settings_are_not_reset_by_legacy_updates(tmp_path):
    store = Store(tmp_path)
    full = store.save_settings(
        "fair",
        False,
        subtitle_style="large",
        video_quality=480,
        max_bandwidth_mbps=4.5,
        orders_paused=True,
        tv_html_overlay=True,
    )
    assert full["video_quality"] == 480
    assert full["orders_paused"] is True
    assert full["tv_html_overlay"] is True

    updated = store.save_settings("fcfs", True)
    assert updated == {**full, "queue_policy": "fcfs", "allow_unsubtitled": True}
    changed = store.save_settings(
        "fair", False, orders_paused=False, tv_html_overlay=False
    )
    assert changed["orders_paused"] is False
    assert changed["tv_html_overlay"] is False
    assert changed["video_quality"] == 480
    assert changed["max_bandwidth_mbps"] == 4.5
