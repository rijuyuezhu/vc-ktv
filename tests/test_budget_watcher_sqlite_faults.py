"""Disk exhaustion writeback must never poison the shared SQLite transaction."""

import sqlite3
import threading

import pytest

from vc_ktv.core.store import Store
from vc_ktv.infra.disk_budget import DiskBudgetExceeded
from vc_ktv.processing.worker import AutoPrep


@pytest.mark.parametrize("fault", ["second_update", "commit"])
def test_budget_watcher_rolls_back_then_recovers(tmp_path, monkeypatch, fault):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Singer')")
        for index, (status, prep_state) in enumerate(
            [
                ("waiting", "downloading"),
                ("ready", "aligning"),
                ("rendering", "manual"),
            ],
            1,
        ):
            store.db.execute(
                """INSERT INTO songs(
                    bvid,page,title,uploader,cover,duration,requester,requested_at,
                    status,prep_state
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"BV{index:010d}",
                    1,
                    "Song",
                    "Uploader",
                    "",
                    10,
                    "g",
                    index,
                    status,
                    prep_state,
                ),
            )
        store.db.commit()

    worker = AutoPrep(store, lambda _: 10, max_workers=1)
    worker.active_tasks[1] = (threading.get_native_id(), threading.Event())
    cancelled = []
    monkeypatch.setattr(
        worker, "cancel_song", lambda song_id: cancelled.append(song_id)
    )
    monkeypatch.setattr(
        worker, "cancel_external_tasks", lambda: cancelled.append("external")
    )

    original = store.db

    class FailOnce:
        update_count = 0
        failures = 0
        rollbacks = 0

        def execute(self, sql, *args):
            if sql.startswith("UPDATE songs"):
                self.update_count += 1
                if fault == "second_update" and self.update_count == 2:
                    self.failures += 1
                    raise sqlite3.OperationalError("injected second UPDATE failure")
            return original.execute(sql, *args)

        def commit(self):
            if fault == "commit" and not self.failures:
                self.failures += 1
                raise sqlite3.OperationalError("injected COMMIT failure")
            return original.commit()

        def rollback(self):
            self.rollbacks += 1
            return original.rollback()

        def __getattr__(self, name):
            return getattr(original, name)

    wrapper = FailOnce()
    store.db = wrapper
    ticks = 0
    checks = 0

    def wait(_):
        nonlocal ticks
        ticks += 1
        return ticks > 2

    def check_budget():
        nonlocal checks
        checks += 1
        if checks == 2:
            # The previous failure must have rolled back all partial updates.
            assert not original.in_transaction
            assert [
                tuple(row)
                for row in original.execute(
                    "SELECT status,prep_state FROM songs ORDER BY id"
                )
            ] == [
                ("waiting", "downloading"),
                ("ready", "aligning"),
                ("rendering", "manual"),
            ]
        raise DiskBudgetExceeded()

    monkeypatch.setattr(worker.stop_signal, "wait", wait)
    monkeypatch.setattr(store.disk_budget, "check", check_budget)
    try:
        worker.watch_disk()
    finally:
        store.db = original

    assert wrapper.failures == wrapper.rollbacks == 1
    assert checks == 2
    assert cancelled == [1, "external", 1, "external"]
    assert not original.in_transaction
    assert [
        tuple(row)
        for row in original.execute("SELECT status,prep_state FROM songs ORDER BY id")
    ] == [
        ("waiting", "unavailable"),
        ("ready", "original"),
        ("ready", "manual"),
    ]
