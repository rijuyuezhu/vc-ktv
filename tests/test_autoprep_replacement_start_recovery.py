"""Failed replacement thread starts must keep a usable, tracked AutoPrep pool."""

import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song
from vc_ktv.application.factory import create_app
from vc_ktv.autoprep import AutoPrep


@pytest.mark.parametrize("failure_kind", ("start", "started_then_error", "constructor"))
@pytest.mark.parametrize("failed_slot", (2, 3))
def test_replenishment_thread_start_failure_preserves_live_workers(
    tmp_path, monkeypatch, failed_slot, failure_kind
):
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", "4")
    entered = threading.Event()
    release = threading.Event()
    downloaded = threading.Event()
    real_loop = AutoPrep.loop
    real_start = threading.Thread.start
    real_thread = threading.Thread
    attempted = []

    def held_first_loop(preparer):
        if threading.current_thread().name == "vc-ktv-auto-prep-1":
            entered.set()
            assert release.wait(10)
        return real_loop(preparer)

    monkeypatch.setattr(AutoPrep, "loop", held_first_loop)
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"test-autoprep-start-failure",
        auto_prepare=True,
    )
    app.state.events.create("Old", "123456")
    worker = app.state.autoprep
    worker.probe = lambda _: 20

    def download(bvid, page, directory):
        downloaded.set()
        file = directory / "source.mp4"
        file.write_bytes(b"fake-video")
        return file

    worker.download = download
    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        initial = list(worker.workers)
        try:
            assert entered.wait(3)

            def partial_stop():
                worker.stop_signal.set()
                for thread in initial[1:]:
                    thread.join(timeout=3)
                assert all(not thread.is_alive() for thread in initial[1:])
                return False

            def fail_one_start(thread):
                if thread.name == f"vc-ktv-auto-prep-{failed_slot}":
                    attempted.append(thread)
                    if failure_kind == "started_then_error":
                        real_start(thread)
                    raise RuntimeError("simulated Thread.start failure")
                return real_start(thread)

            def fail_one_constructor(*args, **kwargs):
                if kwargs.get("name") == f"vc-ktv-auto-prep-{failed_slot}":
                    attempted.append(kwargs["name"])
                    raise RuntimeError("simulated Thread constructor failure")
                return real_thread(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(worker, "stop", partial_stop)
                if failure_kind == "constructor":
                    patch.setattr(threading, "Thread", fail_one_constructor)
                else:
                    patch.setattr(threading.Thread, "start", fail_one_start)
                response = admin.post("/api/admin/events/close")
            assert response.status_code == 409, response.text
            assert len(attempted) == 1
            assert app.state.events.active_id is not None
            assert app.state.autoprep is worker
            assert not worker.stop_signal.is_set()
            assert len(worker.workers) == (
                4 if failure_kind == "started_then_error" else 3
            )
            assert all(t.is_alive() for t in worker.workers)
            assert initial[0] in worker.workers
            assert not any(t in worker.workers for t in initial[1:])
            assert worker.worker in worker.workers and worker.worker.is_alive()

            worker.resume_after_failed_stop()
            assert len(worker.workers) == 4
            assert all(t.is_alive() for t in worker.workers)
            assert len({id(t) for t in worker.workers}) == 4
        finally:
            release.set()

        song_id = add_song(
            app.state.events.current_store, "guest", status="waiting", prep="pending"
        )
        assert downloaded.wait(5), f"worker did not process song #{song_id}"
