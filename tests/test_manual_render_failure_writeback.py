"""Manual render worker must recover from a transient failure-status SQLite fault."""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_render_startup_recovery import ready_render


class FailureWritebackFault:
    def __init__(self, db, mode):
        self.db = db
        self.mode = mode
        self.failures = 0
        self.pending = False
        self.rollbacks = 0

    def execute(self, sql, *args):
        if "UPDATE songs SET status='ready',render_error=?" in sql:
            if self.mode == "execute" and not self.failures:
                self.failures = 1
                raise sqlite3.OperationalError("injected failure status UPDATE fault")
            if self.mode == "commit" and not self.failures:
                self.pending = True
        return self.db.execute(sql, *args)

    def commit(self):
        if self.pending and not self.failures:
            self.pending = False
            self.failures = 1
            raise sqlite3.OperationalError("injected failure status COMMIT fault")
        return self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.db.rollback()

    def __getattr__(self, name):
        return getattr(self.db, name)


@pytest.mark.parametrize("mode", ("execute", "commit"))
def test_manual_render_failure_writeback_recovers_from_transient_sql_fault(
    tmp_path, monkeypatch, mode
):
    app, store, song_id = ready_render(tmp_path)
    threads = []
    started = threading.Event()

    def fail_render(*args):
        threads.append(threading.current_thread())
        started.set()
        raise RuntimeError("simulated manual render failure")

    monkeypatch.setattr("vc_ktv.processing.manual_render.render_video", fail_render)
    real_db = store.db
    injected = FailureWritebackFault(real_db, mode)
    store.db = injected
    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        response = admin.post(f"/api/admin/songs/{song_id}/render", json={"lead": 1.0})
        assert response.status_code == 200, response.text
        assert started.wait(timeout=5), "render worker did not start"
        threads[0].join(timeout=5)
        assert not threads[0].is_alive()
    store.db = real_db

    assert injected.failures == 1
    assert injected.rollbacks == 1
    assert not real_db.in_transaction
    saved = store.rows(
        "SELECT status,render_error,media_file FROM songs WHERE id=?", (song_id,)
    )[0]
    assert saved["status"] == "ready"
    assert saved["render_error"].startswith("制作失败")
    assert saved["media_file"] == "source.mp4"
    assert (store.media / "source.mp4").is_file()
    assert not list(store.media.glob(f"{song_id}-ktv-*.mp4"))
