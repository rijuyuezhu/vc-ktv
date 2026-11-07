"""A failed obsolete-video cleanup must not invalidate a completed render."""

from pathlib import Path

from tests.test_stage_durations import ready_song
from vc_ktv.processing.worker import AutoPrep


def test_obsolete_video_unlink_failure_keeps_committed_new_render(
    tmp_path, monkeypatch
):
    store, source, song_id = ready_song(tmp_path)
    previous = store.media / "previous-baked.mp4"
    previous.write_bytes(b"prior-subtitles")
    with store.lock:
        store.db.execute(
            "UPDATE songs SET media_file=? WHERE id=?", (previous.name, song_id)
        )
        store.db.commit()

    def render(video, lyrics, target, archive, duration, *, preset):
        assert video == source
        target.write_bytes(b"new-finished-subtitles")
        (archive / "aligned.lrc").write_text("[00:00.20]new lyrics", encoding="utf-8")
        return {"rendered": True}

    worker = AutoPrep(store, lambda path: 4, enhance=render)
    row = worker.claim()
    assert row is not None and row["id"] == song_id
    real_unlink = Path.unlink

    def fail_obsolete_delete(path, *args, **kwargs):
        if path == previous:
            raise PermissionError("simulated obsolete media cleanup error")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_obsolete_delete)
    worker.prepare_subtitles(row)
    with store.lock:
        saved = store.db.execute(
            "SELECT prep_state,prep_stage,prep_error,media_file FROM songs WHERE id=?",
            (song_id,),
        ).fetchone()
    assert saved["prep_state"] == "done"
    assert saved["prep_stage"] == "done"
    assert saved["prep_error"] == ""
    assert saved["media_file"] != previous.name
    assert (store.media / saved["media_file"]).read_bytes() == b"new-finished-subtitles"
    assert previous.read_bytes() == b"prior-subtitles"


def test_manual_render_obsolete_unlink_error_keeps_new_video(tmp_path, monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from tests.test_manual_casting_queue import add_song, new_app

    app = new_app(tmp_path)
    store = app.state.store
    song_id = add_song(store, "singer", media="baked.mp4")
    old = store.media / "baked.mp4"
    source = store.media / "source.mp4"
    source.write_bytes(b"source")
    with store.lock:
        store.db.execute(
            "UPDATE songs SET source_file=?,lyrics=? WHERE id=?",
            (source.name, "[00:01.00]first line\n[00:02.00]second line", song_id),
        )
        store.db.commit()

    render_started = threading.Event()
    render_threads = []

    def fake_render(source_path, lyrics, destination, duration, lead):
        destination.write_bytes(b"render complete")
        render_threads.append(threading.current_thread())
        render_started.set()

    monkeypatch.setattr("vc_ktv.api.preparation_admin.render_video", fake_render)
    real_unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == old:
            raise PermissionError("old baked video cannot be removed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with TestClient(app) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        response = admin.post(f"/api/admin/songs/{song_id}/render", json={"lead": 1.0})
        assert response.status_code == 200, response.text
        assert render_started.wait(3), "Renderer did not run"
        render_threads[0].join(timeout=5)
        assert not render_threads[0].is_alive(), "Renderer did not finish cleanup"
        row = store.rows(
            "SELECT status,render_error,media_file FROM songs WHERE id=?", (song_id,)
        )[0]
        assert row["status"] == "ready"
        assert row["render_error"] == ""
        assert row["media_file"] != old.name
        assert (store.media / row["media_file"]).read_bytes() == b"render complete"
        assert old.read_bytes() == b"valid-pretend-mp4"


def test_admin_upload_old_file_cleanup_failure_is_nonfatal(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.test_manual_casting_queue import add_song, new_app

    app = new_app(tmp_path)
    app.state.probe = lambda path: 4
    store = app.state.store
    song_id = add_song(store, "singer", media="prior.mp4")
    previous = store.media / "prior.mp4"
    real_unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == previous:
            raise PermissionError("cannot unlink previous upload")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with TestClient(app) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        response = admin.post(
            f"/api/admin/songs/{song_id}/media",
            files={"file": ("new.mp4", b"new uploaded content", "video/mp4")},
        )
        assert response.status_code == 200
    saved = store.rows(
        "SELECT status,media_file,source_file FROM songs WHERE id=?", (song_id,)
    )[0]
    assert saved["status"] == "ready"
    assert saved["media_file"] == saved["source_file"]
    assert saved["media_file"] != previous.name
    assert (store.media / saved["media_file"]).read_bytes() == b"new uploaded content"
    assert previous.read_bytes() == b"valid-pretend-mp4"
