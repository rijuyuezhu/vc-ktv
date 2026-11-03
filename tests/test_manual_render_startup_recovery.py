"""Failed manual-render startup must not leave a song stuck rendering."""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song, new_app


def ready_render(tmp_path):
    app = new_app(tmp_path)
    store = app.state.events.current_store
    song_id = add_song(store, "singer", media="source.mp4")
    with store.lock:
        store.db.execute(
            "UPDATE songs SET lyrics=? WHERE id=?",
            ("[00:01.00]one\n[00:02.00]two", song_id),
        )
        store.db.commit()
    return app, store, song_id


@pytest.mark.parametrize("fail_at", ("update", "commit"))
def test_startup_sql_failure_does_not_leave_song_rendering(tmp_path, fail_at):
    app, store, song_id = ready_render(tmp_path)
    underlying = store.db

    class FailStartSQL:
        pending = False
        did_fail = False

        def execute(self, sql, args=()):
            if sql.startswith("UPDATE songs SET status='rendering'"):
                if fail_at == "update":
                    self.did_fail = True
                    raise sqlite3.OperationalError("failed render startup UPDATE")
                self.pending = True
            return underlying.execute(sql, args)

        def commit(self):
            if self.pending and not self.did_fail:
                self.did_fail = True
                self.pending = False
                raise sqlite3.OperationalError("failed render startup COMMIT")
            return underlying.commit()

        def __getattr__(self, name):
            return getattr(underlying, name)

    injected = FailStartSQL()
    store.db = injected
    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        response = admin.post(f"/api/admin/songs/{song_id}/render", json={"lead": 1.0})
        assert response.status_code >= 400
        assert injected.did_fail
        saved = store.rows(
            "SELECT status,media_file FROM songs WHERE id=?", (song_id,)
        )[0]
        assert saved["status"] == "ready"
        assert saved["media_file"] == "source.mp4"
        assert not underlying.in_transaction
        assert (store.media / "source.mp4").is_file()
        assert admin.post("/api/admin/events/close").status_code == 200


@pytest.mark.parametrize("fail_at", ("constructor", "start"))
def test_failed_render_thread_start_restores_ready_and_releases_busy(
    tmp_path, monkeypatch, fail_at
):
    app, store, song_id = ready_render(tmp_path)
    original_start = threading.Thread.start
    original_init = threading.Thread.__init__
    failed = []

    def fail_render_start(thread):
        if getattr(getattr(thread, "_target", None), "__name__", "") == "render_worker":
            failed.append(thread)
            raise RuntimeError("simulated render thread start failed")
        return original_start(thread)

    def fail_render_init(thread, *args, **kwargs):
        if getattr(kwargs.get("target"), "__name__", "") == "render_worker":
            failed.append(thread)
            raise RuntimeError("simulated render thread constructor failed")
        return original_init(thread, *args, **kwargs)

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        monkeypatch.setattr(
            threading.Thread,
            "__init__" if fail_at == "constructor" else "start",
            fail_render_init if fail_at == "constructor" else fail_render_start,
        )
        for attempt in range(2):
            response = admin.post(
                f"/api/admin/songs/{song_id}/render", json={"lead": 1.0}
            )
            assert response.status_code == 500, (attempt, response.text)
            assert len(failed) == attempt + 1
            row = store.rows(
                "SELECT status,media_file FROM songs WHERE id=?", (song_id,)
            )[0]
            assert row["status"] == "ready"
            assert row["media_file"] == "source.mp4"
        assert admin.post("/api/admin/events/close").status_code == 200
