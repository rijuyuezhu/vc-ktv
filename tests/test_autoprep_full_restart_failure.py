"""A fully stopped AutoPrep must be recoverable when its replacement cannot start."""

import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song
from vc_ktv.app import create_app


@pytest.mark.parametrize("failure", ("constructor", "before_start", "after_start"))
@pytest.mark.parametrize("failed_slot", (1, 2))
def test_failed_full_replacement_start_preserves_active_service(
    tmp_path, monkeypatch, failed_slot, failure
):
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", "4")
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"test-full-start-failure",
        auto_prepare=True,
    )
    events = app.state.events
    events.create("Old", "123456")
    old_worker = app.state.autoprep
    downloaded = threading.Event()

    def fake_download(bvid, page, directory):
        downloaded.set()
        source = directory / "source.mp4"
        source.write_bytes(b"fake-video")
        return source

    old_worker.download = fake_download
    old_worker.probe = lambda _: 20
    real_start = threading.Thread.start
    real_thread = threading.Thread
    errors = []

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert all(thread.is_alive() for thread in old_worker.workers)
        old_stop = old_worker.stop

        def stopped_but_failed():
            assert old_stop()
            return False

        def fail_start(thread):
            if thread.name == f"vc-ktv-auto-prep-{failed_slot}":
                errors.append(thread.name)
                if failure == "after_start":
                    real_start(thread)
                raise RuntimeError("simulated full replacement start error")
            return real_start(thread)

        def fail_constructor(*args, **kwargs):
            if kwargs.get("name") == f"vc-ktv-auto-prep-{failed_slot}":
                errors.append(kwargs["name"])
                raise RuntimeError("simulated full replacement construction error")
            return real_thread(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(old_worker, "stop", stopped_but_failed)
            if failure == "constructor":
                patch.setattr(threading, "Thread", fail_constructor)
            else:
                patch.setattr(threading.Thread, "start", fail_start)
            response = admin.post("/api/admin/events/close")

        assert response.status_code == 409, response.text
        assert errors
        assert events.active_id is not None
        assert events.metadata()["status"] == "active"
        restored = app.state.autoprep
        assert restored is not old_worker
        assert restored.store is events.current_store
        assert restored.download is fake_download
        assert len(restored.workers) == (4 if failure == "after_start" else 3)
        assert all(t.is_alive() for t in restored.workers)
        assert len({id(t) for t in restored.workers}) == len(restored.workers)
        assert restored.worker and restored.worker.is_alive()
        assert not restored.stop_signal.is_set()
        assert all(not t.is_alive() for t in old_worker.workers)

        restored.resume_after_failed_stop()
        assert len(restored.workers) == 4
        assert all(t.is_alive() for t in restored.workers)
        song_id = add_song(
            events.current_store, "guest", status="waiting", prep="pending"
        )
        assert downloaded.wait(5), f"restored AutoPrep did not process song #{song_id}"


@pytest.mark.parametrize("worker_count", (1, 4))
def test_all_replacement_threads_fail_without_masking_close_error(
    tmp_path, monkeypatch, worker_count
):
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", str(worker_count))
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"test-no-replacement-threads",
        auto_prepare=True,
    )
    events = app.state.events
    events.create("Old", "123456")
    previous = app.state.autoprep
    real_start = threading.Thread.start
    failures = []

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert all(t.is_alive() for t in previous.workers)
        previous_stop = previous.stop

        def fail_after_stop():
            assert previous_stop()
            return False

        def fail_every_worker(thread):
            if thread.name.startswith("vc-ktv-auto-prep-"):
                failures.append(thread.name)
                raise RuntimeError("simulated OS thread resource exhaustion")
            return real_start(thread)

        with monkeypatch.context() as patch:
            patch.setattr(previous, "stop", fail_after_stop)
            patch.setattr(threading.Thread, "start", fail_every_worker)
            response = admin.post("/api/admin/events/close")

        assert response.status_code == 409, response.text
        assert len(failures) == worker_count
        assert events.active_id is not None
        assert events.metadata()["status"] == "active"
        replacement = app.state.autoprep
        assert replacement is not previous
        assert replacement.store is events.current_store
        assert replacement.workers == []
        assert replacement.worker is None
        assert not replacement.stop_signal.is_set()
        assert all(not t.is_alive() for t in previous.workers)

        # A transient failure does not poison a later recovery attempt.
        replacement.resume_after_failed_stop()
        assert len(replacement.workers) == worker_count
        assert all(t.is_alive() for t in replacement.workers)
        assert replacement.worker in replacement.workers
