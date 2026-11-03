"""An unsuccessful event close must not silently disable AutoPrep."""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song
from vc_ktv.app import create_app


@pytest.mark.parametrize("max_workers", (1, 4))
@pytest.mark.parametrize("fail_at", ("pointer", "db_commit", "stop"))
def test_close_failure_resumes_autoprep_and_can_process_new_songs(
    tmp_path, monkeypatch, fail_at, max_workers
):
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", str(max_workers))
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"test-worker-close-fail",
        auto_prepare=True,
    )
    events = app.state.events
    events.create("Old", "123456")
    original_worker = app.state.autoprep
    downloaded = threading.Event()

    def fake_download(bvid, page, directory):
        downloaded.set()
        path = directory / "source.mp4"
        path.write_bytes(b"fake-video")
        return path

    original_worker.download = fake_download
    original_worker.probe = lambda _: 20

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert original_worker.worker and original_worker.worker.is_alive()
        with monkeypatch.context() as patch:
            if fail_at == "pointer":
                from pathlib import Path

                original_unlink = Path.unlink

                def fail_pointer(path, *args, **kwargs):
                    if path == events.pointer:
                        raise PermissionError("simulated close pointer failure")
                    return original_unlink(path, *args, **kwargs)

                patch.setattr(Path, "unlink", fail_pointer)
            elif fail_at == "db_commit":
                real_db = events.current_store.db

                class FailCommit:
                    failed = False

                    def commit(self):
                        if not self.failed:
                            self.failed = True
                            raise sqlite3.OperationalError(
                                "simulated close commit failure"
                            )
                        return real_db.commit()

                    def __getattr__(self, name):
                        return getattr(real_db, name)

                patch.setattr(events.current_store, "db", FailCommit())
            else:
                original_stop = original_worker.stop

                def false_stop():
                    original_stop()
                    return False

                patch.setattr(original_worker, "stop", false_stop)

            response = admin.post("/api/admin/events/close")
        assert response.status_code in (409, 500), response.text
        assert events.active_id is not None
        assert events.metadata()["status"] == "active"
        resumed = app.state.autoprep
        assert resumed.store is events.current_store
        assert resumed.worker and resumed.worker.is_alive()
        assert len(resumed.workers) == max_workers
        assert all(thread.is_alive() for thread in resumed.workers)
        assert not resumed.stop_signal.is_set()
        assert resumed is not original_worker
        song_id = add_song(
            events.current_store, "guest", status="waiting", prep="pending"
        )
        assert downloaded.wait(5), f"worker did not process song #{song_id}"
        # Failed close does not invalidate the event's metadata or ownership.
        assert admin.get("/api/admin/events").json()["active"] == events.active_id


def test_close_failure_with_worker_still_running_clears_stop_signal(
    tmp_path, monkeypatch
):
    from vc_ktv.autoprep import AutoPrep

    entered = threading.Event()
    release = threading.Event()
    downloaded = threading.Event()
    real_loop = AutoPrep.loop

    def gated_loop(worker):
        entered.set()
        assert release.wait(5)
        return real_loop(worker)

    with monkeypatch.context() as patch:
        patch.setattr(AutoPrep, "loop", gated_loop)
        app = create_app(
            root=tmp_path / "room",
            password="test-admin",
            secret=b"test-worker-close-alive",
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
                admin.post(
                    "/api/admin/login", json={"password": "test-admin"}
                ).status_code
                == 200
            )
            try:
                assert entered.wait(3)
                with monkeypatch.context() as stop_patch:
                    stop_patch.setattr(worker, "stop", lambda: False)
                    response = admin.post("/api/admin/events/close")
                assert response.status_code == 409
                assert app.state.autoprep is worker
                assert worker.worker.is_alive()
                assert not worker.stop_signal.is_set()
            finally:
                release.set()
            song_id = add_song(
                app.state.events.current_store,
                "guest",
                status="waiting",
                prep="pending",
            )
            assert downloaded.wait(5), f"original worker did not resume song #{song_id}"


def test_disabled_autoprep_is_not_started_after_close_failure(tmp_path, monkeypatch):
    from pathlib import Path

    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"test-worker-close-disabled",
        auto_prepare=False,
    )
    events = app.state.events
    events.create("Old", "123456")
    original_worker = app.state.autoprep
    original_unlink = Path.unlink

    def fail_pointer(path, *args, **kwargs):
        if path == events.pointer:
            raise PermissionError("simulated pointer unlink failure")
        return original_unlink(path, *args, **kwargs)

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_pointer)
            assert admin.post("/api/admin/events/close").status_code == 500
        assert events.active_id is not None
        assert app.state.autoprep is original_worker
        assert original_worker.worker is None
