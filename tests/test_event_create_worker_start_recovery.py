"""A new event keeps an accurate AutoPrep worker pool if starting threads fails."""

import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song
from vc_ktv.application.factory import create_app


@pytest.mark.parametrize("fault", ("constructor", "before_start", "after_start"))
@pytest.mark.parametrize("failed_slot", (1, 2))
def test_new_event_survives_individual_worker_start_fault(
    tmp_path, monkeypatch, failed_slot, fault
):
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", "4")
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"new-event-worker-fault",
        auto_prepare=True,
    )
    failures = []
    downloaded = threading.Event()
    real_thread = threading.Thread
    real_start = threading.Thread.start

    def fail_constructor(*args, **kwargs):
        if kwargs.get("name") == f"vc-ktv-auto-prep-{failed_slot}":
            failures.append(failed_slot)
            raise RuntimeError("simulated new event worker construction failure")
        return real_thread(*args, **kwargs)

    def fail_start(thread):
        if thread.name == f"vc-ktv-auto-prep-{failed_slot}":
            failures.append(failed_slot)
            if fault == "after_start":
                real_start(thread)
            raise RuntimeError("simulated new event worker start failure")
        return real_start(thread)

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        with monkeypatch.context() as patch:
            if fault == "constructor":
                patch.setattr(threading, "Thread", fail_constructor)
            else:
                patch.setattr(threading.Thread, "start", fail_start)
            response = admin.post(
                "/api/admin/events", json={"name": "startup failure", "code": "123456"}
            )

        assert response.status_code == 200, response.text
        assert len(failures) == 1
        events = app.state.events
        entry = response.json()
        assert events.active_id == entry["id"]
        assert events.metadata()["status"] == "active"
        worker = app.state.autoprep
        assert worker.store is events.current_store
        assert len(worker.workers) == (4 if fault == "after_start" else 3)
        assert worker.worker and worker.worker.is_alive()
        assert all(t.is_alive() for t in worker.workers)
        assert len(set(worker.workers)) == len(worker.workers)
        assert not worker.stop_signal.is_set()

        # The missing slot can be filled without stopping the active event.
        worker.resume_after_failed_stop()
        assert len(worker.workers) == 4
        assert all(t.is_alive() for t in worker.workers)

        def fake_download(bvid, page, directory):
            downloaded.set()
            source = directory / "source.mp4"
            source.write_bytes(b"mock-video")
            return source

        worker.download = fake_download
        worker.probe = lambda _: 20
        song_id = add_song(
            events.current_store, "guest", status="waiting", prep="pending"
        )
        assert downloaded.wait(5), f"song #{song_id} not processed"


@pytest.mark.parametrize("worker_count", (1, 4))
def test_new_event_reports_when_no_worker_can_start(
    tmp_path, monkeypatch, worker_count
):
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", str(worker_count))
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"new-event-no-workers",
        auto_prepare=True,
    )
    fail_attempts = []
    real_start = threading.Thread.start

    def fail_start(thread):
        if thread.name.startswith("vc-ktv-auto-prep-"):
            fail_attempts.append(thread.name)
            raise RuntimeError("simulated no available threads")
        return real_start(thread)

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "start", fail_start)
            response = admin.post("/api/admin/events", json={"name": "No workers"})
        # Room creation succeeded: do not misreport a committed event as HTTP 503.
        assert response.status_code == 200, response.text
        assert response.json()["id"]
        assert len(fail_attempts) == worker_count
        events = app.state.events
        assert events.active_id is not None
        assert events.metadata()["status"] == "active"
        assert events.pointer.read_text().strip() == events.active_id
        assert admin.get("/api/admin/events").json()["active"] == events.active_id
        assert (
            admin.post("/api/admin/events", json={"name": "duplicate"}).status_code
            == 409
        )
        worker = app.state.autoprep
        assert worker.store is events.current_store
        assert worker.workers == []
        assert worker.worker is None
        assert admin.get("/api/health").json()["autoprep_ready"] is False
        worker.resume_after_failed_stop()
        assert admin.get("/api/health").json()["autoprep_ready"] is True
        assert len(worker.workers) == worker_count
        assert all(t.is_alive() for t in worker.workers)


def test_new_event_does_not_start_workers_when_autoprep_disabled(tmp_path, monkeypatch):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"new-event-no-auto",
        auto_prepare=False,
    )
    real_start = threading.Thread.start

    def reject_worker_start(thread):
        if thread.name.startswith("vc-ktv-auto-prep-"):
            raise AssertionError(
                "disabled automatic preparation must not start workers"
            )
        return real_start(thread)

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "start", reject_worker_start)
            response = admin.post("/api/admin/events", json={"name": "manual event"})
        assert response.status_code == 200, response.text
        assert app.state.events.active_id == response.json()["id"]
        assert app.state.autoprep.workers == []
        assert app.state.autoprep.worker is None
