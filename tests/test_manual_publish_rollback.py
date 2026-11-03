"""Manual render publication errors must not persist a deleted video path."""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song, new_app


@pytest.mark.parametrize("previously_baked", (False, True))
@pytest.mark.parametrize("fail_at", ("execute", "commit"))
def test_manual_render_publication_failure_keeps_previous_video(
    tmp_path, monkeypatch, previously_baked, fail_at
):
    app = new_app(tmp_path)
    store = app.state.events.current_store
    song_id = add_song(store, "singer", media="previous.mp4")
    previous = store.media / "previous.mp4"
    if previously_baked:
        original = store.media / "source.mp4"
        original.write_bytes(b"original-video")
    else:
        original = previous
    with store.lock:
        store.db.execute(
            "UPDATE songs SET lyrics=?,source_file=? WHERE id=?",
            ("[00:01.00]one\n[00:02.00]two", original.name, song_id),
        )
        store.db.commit()

    worker_threads = []
    underlying = store.db

    class FailPublication:
        committing_media = False
        did_fail = False

        def execute(self, sql, args=()):
            if sql.startswith("UPDATE songs SET status='ready',media_file="):
                if fail_at == "execute":
                    self.did_fail = True
                    raise sqlite3.OperationalError(
                        "simulated manual render UPDATE failure"
                    )
                self.committing_media = True
            return underlying.execute(sql, args)

        def commit(self):
            if self.committing_media and not self.did_fail:
                self.did_fail = True
                self.committing_media = False
                raise sqlite3.OperationalError("simulated manual render COMMIT failure")
            return underlying.commit()

        def __getattr__(self, name):
            return getattr(underlying, name)

    fault = FailPublication()
    store.db = fault

    def fake_render(source, lyrics, destination, duration, lead):
        worker_threads.append(threading.current_thread())
        destination.write_bytes(b"new-video")

    monkeypatch.setattr("vc_ktv.app.render_video", fake_render)

    with TestClient(app) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        response = admin.post(f"/api/admin/songs/{song_id}/render", json={"lead": 1.0})
        assert response.status_code == 200, response.text
        assert worker_threads
        worker_threads[0].join(timeout=5)
        assert not worker_threads[0].is_alive()

    assert fault.did_fail
    saved = store.rows(
        "SELECT status,render_error,media_file FROM songs WHERE id=?", (song_id,)
    )[0]
    assert saved["status"] == "ready"
    assert saved["render_error"].startswith("制作失败")
    assert saved["media_file"] == previous.name
    assert previous.read_bytes() == b"valid-pretend-mp4"
    assert original.is_file()
    assert not list(store.media.glob(f"{song_id}-ktv-*.mp4"))
    assert not underlying.in_transaction
