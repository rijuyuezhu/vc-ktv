"""Partially stopped AutoPrep pools must regain their original concurrency."""

import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song
from vc_ktv.application.factory import create_app
from vc_ktv.autoprep import AutoPrep


@pytest.mark.parametrize(
    ("worker_count", "survivor_slots"),
    ((2, (2,)), (3, (2,)), (4, (4,)), (4, (1, 2)), (4, (2, 4)), (4, (1, 2, 3))),
)
def test_partial_stop_close_failure_replenishes_live_pool(
    tmp_path, monkeypatch, worker_count, survivor_slots
):
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", str(worker_count))
    entered = {index: threading.Event() for index in survivor_slots}
    release = threading.Event()
    downloaded = threading.Event()
    real_loop = AutoPrep.loop

    def pause_first_loop(preparer):
        index = int(threading.current_thread().name.rsplit("-", 1)[-1])
        if index in entered:
            entered[index].set()
            assert release.wait(10)
        return real_loop(preparer)

    monkeypatch.setattr(AutoPrep, "loop", pause_first_loop)
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"test-worker-partial-close",
        auto_prepare=True,
    )
    app.state.events.create("Old", "123456")
    worker = app.state.autoprep
    worker.probe = lambda _: 20

    def fake_download(bvid, page, directory):
        downloaded.set()
        path = directory / "source.mp4"
        path.write_bytes(b"fake-video")
        return path

    worker.download = fake_download

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        initial_threads = list(worker.workers)
        try:
            assert all(event.wait(3) for event in entered.values())
            assert all(thread.is_alive() for thread in initial_threads)

            def partially_stop():
                worker.stop_signal.set()
                for index, thread in enumerate(initial_threads, 1):
                    if index not in survivor_slots:
                        thread.join(timeout=4)
                assert all(
                    thread.is_alive() == (index in survivor_slots)
                    for index, thread in enumerate(initial_threads, 1)
                )
                return False

            with monkeypatch.context() as patch:
                patch.setattr(worker, "stop", partially_stop)
                response = admin.post("/api/admin/events/close")
            assert response.status_code == 409, response.text
            assert app.state.events.metadata()["status"] == "active"
            assert app.state.autoprep is worker
            assert not worker.stop_signal.is_set()
            assert len(worker.workers) == worker_count
            assert all(t.is_alive() for t in worker.workers)
            assert all(
                (old in worker.workers) == (index in survivor_slots)
                for index, old in enumerate(initial_threads, 1)
            )
            assert worker.worker in worker.workers and worker.worker.is_alive()
        finally:
            release.set()

        song_id = add_song(
            app.state.events.current_store, "guest", status="waiting", prep="pending"
        )
        assert downloaded.wait(5), f"resumed pool did not process song #{song_id}"
