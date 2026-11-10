"""Manual TV cache regeneration must never rerender a playable song."""

import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

import vc_ktv.processing.worker as worker_module
from tests.test_tv_html_source import room, setup_song


def setup(tmp_path):
    app = room(tmp_path)
    song_id, source, baked, archive = setup_song(app, source_size=600_000, duration=1)
    app.state.autoprep.probe = lambda source: 1.0
    return app, song_id, source, baked, archive


def login(client):
    assert client.post("/api/admin/login", json={"password": "pw"}).status_code == 200


def wait_for_retry(app):
    thread = app.state.lifecycle.worker.cache_retry_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_retry_api_requires_admin_and_ready_song(tmp_path):
    app, song_id, source, _, _ = setup(tmp_path)
    with TestClient(app) as owner, TestClient(app) as guest:
        assert (
            guest.post(f"/api/admin/songs/{song_id}/tv-cache/retry").status_code == 401
        )
        login(owner)
        assert owner.post("/api/admin/songs/999/tv-cache/retry").status_code == 404
        with app.state.store.lock:
            app.state.store.db.execute(
                "UPDATE songs SET prep_state='aligning' WHERE id=?", (song_id,)
            )
            app.state.store.db.commit()
        assert (
            owner.post(f"/api/admin/songs/{song_id}/tv-cache/retry").status_code == 409
        )


def test_low_bitrate_and_existing_compatible_cache_are_noops(tmp_path, monkeypatch):
    app, song_id, source, _, archive = setup(tmp_path)
    source.write_bytes(b"x" * 4_000)

    def forbidden(*args, **kwargs):
        raise AssertionError("must not launch re-encoding")

    monkeypatch.setattr(worker_module, "prepare_tv_source", forbidden)
    with TestClient(app) as admin:
        login(admin)
        url = f"/api/admin/songs/{song_id}/tv-cache/retry"
        result = admin.post(url)
        assert result.status_code == 200
        assert result.json()["status"] == "not_needed"
        assert app.state.autoprep.cache_retry_thread is None
        source.write_bytes(b"x" * 600_000)
        (archive / "tv-source.mp4").write_bytes(b"VALID CACHE" * 200)
        assert admin.post(url).json()["status"] == "already_ready"
        assert app.state.autoprep.cache_retry_thread is None


def test_cache_retry_publishes_atomically_without_rerender(tmp_path, monkeypatch):
    app, song_id, source, baked, archive = setup(tmp_path)
    original = source.read_bytes()
    rendered = baked.read_bytes()
    published = []

    def fake_cache(source_path, destination, duration, bandwidth, **kwargs):
        assert source_path == source
        assert duration == 1.0
        assert bandwidth == 3.0
        assert destination != archive
        assert not (archive / "tv-source.mp4").exists()
        (destination / "tv-source.mp4").write_bytes(b"NEW CACHE" * 2000)
        published.append(True)

    monkeypatch.setattr(worker_module, "prepare_tv_source", fake_cache)
    with TestClient(app) as admin:
        login(admin)
        url = f"/api/admin/songs/{song_id}/tv-cache/retry"
        assert admin.post(url).json()["status"] == "started"
        wait_for_retry(app)
        assert published == [True]
        assert (archive / "tv-source.mp4").read_bytes() == b"NEW CACHE" * 2000
        metadata = json.loads((archive / "attempt.json").read_text())
        assert metadata["outcome"] == "rendered"
        assert metadata["source_file"] == source.name
        assert metadata["tv_source_ready"] is True
        assert metadata["tv_cache_seconds"] >= 0
        assert (
            app.state.media.overlay_source(
                dict(
                    app.state.store.db.execute(
                        "SELECT * FROM songs WHERE id=?", (song_id,)
                    ).fetchone()
                )
            )
            == archive / "tv-source.mp4"
        )
        assert admin.post(url).json()["status"] == "already_ready"
    assert source.read_bytes() == original
    assert baked.read_bytes() == rendered
    assert not list(archive.glob("tv-cache-retry-*"))


def test_failed_cache_retry_keeps_baked_and_can_retry(tmp_path, monkeypatch):
    app, song_id, _, baked, archive = setup(tmp_path)
    attempts = []

    def flaky(source, destination, duration, bandwidth, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            (destination / "tv-source.mp4").write_bytes(b"PARTIAL")
            raise OSError("encoder failed")
        (destination / "tv-source.mp4").write_bytes(b"GOOD" * 1024)

    monkeypatch.setattr(worker_module, "prepare_tv_source", flaky)
    with TestClient(app) as admin:
        login(admin)
        url = f"/api/admin/songs/{song_id}/tv-cache/retry"
        assert admin.post(url).json()["status"] == "started"
        wait_for_retry(app)
        assert not (archive / "tv-source.mp4").exists()
        assert baked.read_bytes() == b"BAKED"
        assert admin.post(url).json()["status"] == "started"
        wait_for_retry(app)
        assert (archive / "tv-source.mp4").read_bytes() == b"GOOD" * 1024
        assert len(attempts) == 2


def test_duplicate_request_and_song_cancellation(tmp_path, monkeypatch):
    app, song_id, _, baked, archive = setup(tmp_path)
    started, release = threading.Event(), threading.Event()

    def slow_cache(source, destination, duration, bandwidth, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        (destination / "tv-source.mp4").write_bytes(b"UNCOMMITTED")

    monkeypatch.setattr(worker_module, "prepare_tv_source", slow_cache)
    try:
        with TestClient(app) as admin:
            login(admin)
            url = f"/api/admin/songs/{song_id}/tv-cache/retry"
            assert admin.post(url).json()["status"] == "started"
            assert started.wait(timeout=4)
            assert admin.post(url).status_code == 409
            app.state.lifecycle.stop_song(song_id)
            release.set()
            wait_for_retry(app)
            assert not (archive / "tv-source.mp4").exists()
            assert baked.read_bytes() == b"BAKED"
    finally:
        release.set()


def test_changed_bandwidth_does_not_publish_stale_cache(tmp_path, monkeypatch):
    app, song_id, _, _, archive = setup(tmp_path)
    started, release = threading.Event(), threading.Event()

    def slow_cache(source, destination, duration, bandwidth, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        (destination / "tv-source.mp4").write_bytes(b"STALE")

    monkeypatch.setattr(worker_module, "prepare_tv_source", slow_cache)
    try:
        with TestClient(app) as admin:
            login(admin)
            assert (
                admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry").json()[
                    "status"
                ]
                == "started"
            )
            assert started.wait(timeout=4)
            app.state.store.save_settings(None, None, max_bandwidth_mbps=2.0)
            release.set()
            wait_for_retry(app)
            assert not (archive / "tv-source.mp4").exists()
    finally:
        release.set()


def test_event_close_cancels_cache_retry_before_closing_database(tmp_path, monkeypatch):
    app, song_id, _, _, archive = setup(tmp_path)
    entered = threading.Event()

    def wait_for_cancel(source, destination, duration, bandwidth, **kwargs):
        entered.set()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            cancelled = app.state.autoprep.cache_retry_cancel
            if cancelled and cancelled.is_set():
                return
            time.sleep(0.01)
        raise AssertionError("event did not cancel cache retry")

    monkeypatch.setattr(worker_module, "prepare_tv_source", wait_for_cancel)
    with TestClient(app) as admin:
        login(admin)
        assert (
            admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry").json()["status"]
            == "started"
        )
        assert entered.wait(timeout=4)
        result = admin.post("/api/admin/events/close")
        assert result.status_code == 200, result.text
        thread = app.state.autoprep.cache_retry_thread
        assert thread is not None and not thread.is_alive()
        assert app.state.events.active_id is None
        assert not (archive / "tv-source.mp4").exists()


def test_cache_retry_source_replacement_invalidates_pending_result(
    tmp_path, monkeypatch
):
    app, song_id, source, baked, archive = setup(tmp_path)
    started, release = threading.Event(), threading.Event()

    def slow_cache(source, destination, duration, bandwidth, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        (destination / "tv-source.mp4").write_bytes(b"WRONG SOURCE")

    monkeypatch.setattr(worker_module, "prepare_tv_source", slow_cache)
    try:
        with TestClient(app) as admin:
            login(admin)
            assert (
                admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry").json()[
                    "status"
                ]
                == "started"
            )
            assert started.wait(timeout=4)
            with app.state.store.lock:
                app.state.store.db.execute(
                    "UPDATE songs SET media_file=? WHERE id=?",
                    ("other-render.mp4", song_id),
                )
                app.state.store.db.commit()
            release.set()
            wait_for_retry(app)
            assert not (archive / "tv-source.mp4").exists()
            assert baked.is_file() and source.is_file()
    finally:
        release.set()


def test_cache_retry_disk_budget_rejected_before_start(tmp_path):
    app, song_id, _, _, _ = setup(tmp_path)
    with TestClient(app) as admin:
        login(admin)
        app.state.store.disk_budget.limit_bytes = 100
        response = admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry")
        assert response.status_code == 409, response.text
        assert app.state.autoprep.cache_retry_thread is None


def test_cache_retry_rejects_song_with_initial_worker_still_active(tmp_path):
    app, song_id, _, _, _ = setup(tmp_path)
    worker = app.state.autoprep
    with TestClient(app) as admin:
        login(admin)
        with worker.active_tasks_lock:
            worker.active_tasks[song_id] = (
                threading.get_native_id(),
                threading.Event(),
            )
        try:
            response = admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry")
            assert response.status_code == 409
            assert worker.cache_retry_thread is None
        finally:
            with worker.active_tasks_lock:
                worker.active_tasks.pop(song_id)


def test_cache_retry_failed_atomic_install_preserves_previous_cache(
    tmp_path, monkeypatch
):
    app, song_id, _, baked, archive = setup(tmp_path)
    existing = archive / "tv-source.mp4"
    existing.write_bytes(b"PREVIOUS CACHE")
    # An older cache is now invalid under the stricter current bandwidth budget.
    app.state.store.save_settings(None, None, max_bandwidth_mbps=2.0)
    original_replace = Path.replace

    def reject_publish(path, dest):
        if path.name == "tv-source.mp4" and Path(dest) == existing:
            raise PermissionError("simulated atomic cache publication failure")
        return original_replace(path, dest)

    def cache(source, destination, duration, bandwidth, **kwargs):
        (destination / "tv-source.mp4").write_bytes(b"NEW CACHE")

    monkeypatch.setattr(worker_module, "prepare_tv_source", cache)
    monkeypatch.setattr(Path, "replace", reject_publish)
    with TestClient(app) as admin:
        login(admin)
        assert (
            admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry").json()["status"]
            == "started"
        )
        wait_for_retry(app)
        assert existing.read_bytes() == b"PREVIOUS CACHE"
        assert baked.read_bytes() == b"BAKED"
        assert not list(archive.glob("tv-cache-retry-*"))


def test_service_shutdown_stops_cache_retry_without_autoprep(tmp_path, monkeypatch):
    app, song_id, _, _, archive = setup(tmp_path)
    started = threading.Event()

    def cancellable(source, destination, duration, bandwidth, **kwargs):
        started.set()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            cancel = app.state.autoprep.cache_retry_cancel
            if cancel and cancel.is_set():
                return
            time.sleep(0.01)
        raise AssertionError("shutdown did not cancel cache work")

    monkeypatch.setattr(worker_module, "prepare_tv_source", cancellable)
    with TestClient(app) as admin:
        login(admin)
        assert (
            admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry").json()["status"]
            == "started"
        )
        assert started.wait(timeout=4)
    thread = app.state.autoprep.cache_retry_thread
    assert thread is not None and not thread.is_alive()
    assert not (archive / "tv-source.mp4").exists()


def test_incomplete_cache_is_not_served_or_treated_as_ready(tmp_path, monkeypatch):
    app, song_id, _, _, archive = setup(tmp_path)
    (archive / "tv-source.mp4").write_bytes(b"PARTIAL")
    row = dict(
        app.state.store.db.execute(
            "SELECT * FROM songs WHERE id=?", (song_id,)
        ).fetchone()
    )
    assert app.state.media.overlay_source(row) is None

    def repair(source, staging, duration, bandwidth, **kwargs):
        (staging / "tv-source.mp4").write_bytes(b"RECOVERED" * 400)

    monkeypatch.setattr(worker_module, "prepare_tv_source", repair)
    with TestClient(app) as admin:
        login(admin)
        response = admin.post(f"/api/admin/songs/{song_id}/tv-cache/retry")
        assert response.status_code == 200
        assert response.json()["status"] == "started"
        wait_for_retry(app)
        assert (archive / "tv-source.mp4").read_bytes() == b"RECOVERED" * 400
        assert app.state.media.overlay_source(row) == archive / "tv-source.mp4"
