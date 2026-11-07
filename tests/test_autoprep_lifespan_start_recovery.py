"""Existing active events survive a fault while AutoPrep starts in lifespan."""

import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song
from vc_ktv.application.factory import create_app


def existing_event(tmp_path, monkeypatch, workers):
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", str(workers))
    root = tmp_path / "room"
    app = create_app(
        root=root,
        password="test-admin",
        secret=b"boot-worker-fault",
        auto_prepare=False,
    )
    entry = app.state.events.create("Existing room", "123456")
    app.state.events.current_store.db.close()
    restarted = create_app(
        root=root, password="test-admin", secret=b"boot-worker-fault", auto_prepare=True
    )
    assert restarted.state.events.active_id == entry["id"]
    return restarted


@pytest.mark.parametrize("fault", ("constructor", "before_start", "after_start"))
@pytest.mark.parametrize("failed_slot", (1, 2))
def test_lifespan_partial_worker_start_failure_keeps_api_and_workers(
    tmp_path, monkeypatch, failed_slot, fault
):
    app = existing_event(tmp_path, monkeypatch, 4)
    worker = app.state.autoprep
    real_thread = threading.Thread
    real_start = threading.Thread.start
    failures = []
    downloaded = threading.Event()
    worker.probe = lambda _: 20

    def fake_download(bvid, page, directory):
        downloaded.set()
        path = directory / "source.mp4"
        path.write_bytes(b"fake-video")
        return path

    worker.download = fake_download

    def failed_constructor(*args, **kwargs):
        if kwargs.get("name") == f"vc-ktv-auto-prep-{failed_slot}" and not failures:
            failures.append(failed_slot)
            raise RuntimeError("simulated Thread constructor failure at app boot")
        return real_thread(*args, **kwargs)

    def failed_start(thread):
        if thread.name == f"vc-ktv-auto-prep-{failed_slot}" and not failures:
            failures.append(failed_slot)
            if fault == "after_start":
                real_start(thread)
            raise RuntimeError("simulated Thread.start failure at app boot")
        return real_start(thread)

    try:
        with monkeypatch.context() as patch:
            if fault == "constructor":
                patch.setattr(threading, "Thread", failed_constructor)
            else:
                patch.setattr(threading.Thread, "start", failed_start)
            with TestClient(app) as client:
                assert client.get("/api/health").status_code == 200
                assert client.get("/api/health").json()["autoprep_workers"] == (
                    4 if fault == "after_start" else 3
                )
                assert (
                    client.get("/api/event").json()["event"]["id"]
                    == app.state.events.active_id
                )
                assert worker.store is app.state.events.current_store
                assert worker.worker and worker.worker.is_alive()
                assert len(worker.workers) == (4 if fault == "after_start" else 3)
                assert all(t.is_alive() for t in worker.workers)
                assert len(set(worker.workers)) == len(worker.workers)
                assert not worker.stop_signal.is_set()
                assert failures == [failed_slot]

                worker.resume_after_failed_stop()
                assert len(worker.workers) == 4
                song_id = add_song(
                    app.state.events.current_store,
                    "guest",
                    status="waiting",
                    prep="pending",
                )
                assert downloaded.wait(5), f"no worker processed queued song #{song_id}"
    finally:
        worker.stop_signal.set()
        for thread in worker.workers:
            if thread.ident is not None:
                thread.join(timeout=2)


@pytest.mark.parametrize("workers", (1, 4))
def test_lifespan_no_worker_can_start_still_serves_active_event(
    tmp_path, monkeypatch, workers
):
    app = existing_event(tmp_path, monkeypatch, workers)
    worker = app.state.autoprep
    real_start = threading.Thread.start
    failed = []

    def no_threads(thread):
        if thread.name.startswith("vc-ktv-auto-prep-") and len(failed) < workers:
            failed.append(thread.name)
            raise RuntimeError("simulated OS thread exhaustion during boot")
        return real_start(thread)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "start", no_threads)
            with TestClient(app) as client:
                health = client.get("/api/health")
                assert health.status_code == 200
                assert health.json()["autoprep_workers"] == 0
                assert health.json()["auto_prepare"] is True
                assert (
                    client.get("/api/event").json()["event"]["id"]
                    == app.state.events.active_id
                )
                assert len(failed) == workers
                assert worker.workers == []
                assert worker.worker is None
                assert worker.store is app.state.events.current_store

                # The service does not invent a healthy worker or lose the event.
                assert app.state.events.metadata()["status"] == "active"
                assert app.state.events.pointer.exists()
                worker.resume_after_failed_stop()
                assert len(worker.workers) == workers
                assert all(thread.is_alive() for thread in worker.workers)
                assert client.get("/api/health").json()["autoprep_workers"] == workers
    finally:
        worker.stop_signal.set()
        for thread in worker.workers:
            if thread.ident is not None:
                thread.join(timeout=2)


def test_lifespan_restart_replays_interrupted_source_download(tmp_path, monkeypatch):
    app = existing_event(tmp_path, monkeypatch, 2)
    worker = app.state.autoprep
    store = app.state.events.current_store
    song_id = add_song(store, "guest", status="waiting", prep="downloading")
    downloaded = threading.Event()

    def fake_download(bvid, page, directory):
        downloaded.set()
        path = directory / "source.mp4"
        path.write_bytes(b"fake-video")
        return path

    worker.download = fake_download
    worker.probe = lambda _: 20

    with TestClient(app) as client:
        assert downloaded.wait(5), f"interrupted song #{song_id} was not recovered"
        assert client.get("/api/health").json()["autoprep_workers"] == 2
        with store.lock:
            assert (
                store.db.execute(
                    "SELECT prep_state FROM songs WHERE id=?", (song_id,)
                ).fetchone()[0]
                != "pending"
            )


def test_lifespan_boot_without_event_starts_no_workers(tmp_path):
    app = create_app(
        root=tmp_path / "empty-room",
        password="test-admin",
        secret=b"boot-no-event",
        auto_prepare=True,
    )
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["autoprep_workers"] == 0
        assert app.state.autoprep.worker is None
        assert app.state.autoprep.workers == []
