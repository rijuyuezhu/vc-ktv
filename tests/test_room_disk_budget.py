"""The room budget protects aggregate storage without per-file size limits."""

import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.application.factory import create_app
from vc_ktv.core.store import Store
from vc_ktv.infra.disk_budget import DiskBudgetExceeded, EventDiskBudget, directory_size
from vc_ktv.processing.autoprep import AutoPrep


def eventually(predicate, seconds=6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_budget_counts_nested_intermediate_files_not_symlink_targets(tmp_path):
    room = tmp_path / "room"
    work = room / "media" / "1-alignment" / "attempt-1"
    work.mkdir(parents=True)
    (room / "media" / "1-original.mp4").write_bytes(b"x" * 800)
    (work / "work.wav").write_bytes(b"y" * 600)
    (room / "media" / "alias").symlink_to(tmp_path / "external")
    (tmp_path / "external").write_bytes(b"z" * 10000)
    assert directory_size(room) == 1400
    budget = EventDiskBudget(room, limit_bytes=1500)
    budget.check()
    with pytest.raises(DiskBudgetExceeded):
        budget.check(additional_bytes=101)
    (work / "work.wav").unlink()
    budget.check(additional_bytes=101)


def test_room_budget_blocks_new_orders_but_allows_again_after_cleanup(tmp_path):
    app = create_app(
        root=tmp_path / "ktv", password="pw", secret=b"event-budget", auto_prepare=False
    )
    app.state.events.create("Test", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "A",
            "uploader": "Uploader",
            "cover": "",
            "duration": 10,
        }

    app.state.lookup = lookup
    with TestClient(app) as guest:
        join_and_register(guest, "Singer")
        active = app.state.events.active()
        active.disk_budget.limit_bytes = active.disk_budget.used_bytes() + 2048
        extra = active.root / "large-work.tmp"
        extra.write_bytes(b"x" * 4096)
        assert (
            guest.post("/api/songs", json={"bvid": "BV0000000002"}).status_code == 507
        )
        extra.unlink()
        result = guest.post("/api/songs", json={"bvid": "BV0000000002"})
        assert result.status_code == 200, result.text


def test_budget_stops_active_worker_when_intermediate_files_fill_room(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES ('g','Singer')")
        store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,status,prep_state) "
            "VALUES('BV0000000002',1,'A','Uploader','',20,'g',1,'waiting','pending')"
        )
        store.db.commit()
    store.disk_budget.limit_bytes = store.disk_budget.used_bytes() + 1_000_000
    started = threading.Event()
    spawned = []

    def download(_bvid, _page, directory):
        (directory / "large-work.tmp").write_bytes(b"x" * 1_500_000)
        process = subprocess.Popen(["sleep", "30"])
        spawned.append(process)
        started.set()
        assert process.wait() == 0  # The watcher should terminate this subprocess.
        path = directory / "source.mp4"
        path.write_bytes(b"video")
        return path

    worker = AutoPrep(store, probe=lambda _: 20, download=download, max_workers=1)
    worker.start()
    try:
        assert started.wait(3)
        assert eventually(lambda: spawned[0].poll() is not None)
        assert spawned[0].returncode == -9
        assert eventually(lambda: worker.active_claims == 0)
        row = store.db.execute(
            "SELECT prep_state,prep_error,prep_attempts FROM songs WHERE id=1"
        ).fetchone()
        assert row["prep_state"] == "unavailable"
        assert "200 GiB" in row["prep_error"]
        assert row["prep_attempts"] == 3
        assert not list(store.root.glob("vc-ktv-1-*"))
    finally:
        worker.stop()


def test_budget_exceeded_during_download_is_not_retried(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Singer')")
        store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,status,prep_state) "
            "VALUES('BV0000000002',1,'A','Uploader','',20,'g',1,'waiting','pending')"
        )
        store.db.commit()

    def download(_bvid, _page, directory):
        path = directory / "source.mp4"
        path.write_bytes(b"x" * 4000)
        return path

    worker = AutoPrep(store, lambda _: 20, download=download)
    claim = worker.claim()
    assert claim and claim["id"] == 1
    store.disk_budget.limit_bytes = store.disk_budget.used_bytes() + 500
    try:
        with pytest.raises(DiskBudgetExceeded):
            worker.prepare_source(claim)
        worker._record_failure(claim, DiskBudgetExceeded(), time.monotonic())
        row = store.db.execute(
            "SELECT status,prep_state,prep_attempts,prep_error FROM songs WHERE id=1"
        ).fetchone()
        assert row["status"] == "waiting" and row["prep_state"] == "unavailable"
        assert row["prep_attempts"] == 3 and "200 GiB" in row["prep_error"]
        assert not list(store.root.glob("vc-ktv-1-*"))
    finally:
        with store.lock:
            worker.active_claims -= 1
            worker.busy.clear()
