"""Direct Karaoke admin status, timings, retry, and forced-ready integration."""

import sqlite3
import time

from fastapi.testclient import TestClient

from tests.test_require_backing_ready import add_ready, complete_backing, event
from vc_ktv.application.factory import create_app
from vc_ktv.core.store import Store
from vc_ktv.processing.backing_audio import KARAOKE_SHA256, source_identity


class LiveThread:
    def is_alive(self):
        return True


def login(admin):
    response = admin.post("/api/admin/login", json={"password": "pw"})
    assert response.status_code == 200, response.text


def setup_song(tmp_path, monkeypatch):
    app = event(tmp_path, monkeypatch)
    song_id = add_ready(app)
    source = app.state.store.media / "video.mp4"
    key = source_identity(source, KARAOKE_SHA256)
    return app, song_id, key


def record(app, key, status, *, duration=None, error="", started_at=None):
    store = app.state.store
    with store.lock:
        store.db.execute(
            """INSERT INTO backing_tracks
               (cache_key,source_file,model_sha256,status,error,elapsed_seconds,updated_at)
               VALUES(?,'video.mp4',?,?,?,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET
                  status=excluded.status,error=excluded.error,
                  elapsed_seconds=excluded.elapsed_seconds,updated_at=excluded.updated_at""",
            (
                key,
                KARAOKE_SHA256,
                status,
                error,
                duration,
                started_at or int(time.time()),
            ),
        )
        store.db.commit()


def test_admin_detail_shows_persisted_backing_timings_and_size(tmp_path, monkeypatch):
    app, song_id, key = setup_song(tmp_path, monkeypatch)
    # This isolated test doesn't run workers; simulate one without a GPU.
    app.state.autoprep.backing.thread = LiveThread()
    with TestClient(app) as admin:
        login(admin)
        detail = admin.get(f"/api/songs/{song_id}/detail").json()
        assert detail["backing"]["status"] == "pending"
        assert detail["backing"]["elapsed_seconds"] is None

        started = int(time.time()) - 16
        record(app, key, "processing", started_at=started)
        detail = admin.get(f"/api/songs/{song_id}/detail").json()
        assert detail["backing"]["status"] == "processing"
        assert detail["backing"]["started_at"] == started
        assert detail["backing"]["elapsed_seconds"] is None

        complete_backing(app)
        record(app, key, "ready", duration=75.35)
        detail = admin.get(f"/api/songs/{song_id}/detail").json()
        assert detail["backing"]["status"] == "ready"
        assert detail["backing"]["elapsed_seconds"] == 75.35
        assert detail["backing"]["started_at"] is None
        assert detail["backing"]["size_bytes"] > 1024
        assert detail["backing"]["error"] == ""
        assert detail["playable"]

        record(app, key, "failed", error="RuntimeError", duration=13.25)
        detail = admin.get(f"/api/songs/{song_id}/detail").json()
        assert detail["backing"]["status"] == "failed"
        assert detail["backing"]["elapsed_seconds"] == 13.25
        assert detail["backing"]["size_bytes"] is None
        assert detail["backing"]["error"] == "RuntimeError"


def test_admin_retry_auth_state_and_forced_queue_recovery(tmp_path, monkeypatch):
    app, song_id, key = setup_song(tmp_path, monkeypatch)
    app.state.autoprep.backing.thread = LiveThread()
    record(app, key, "failed", error="ValueError", duration=18.0)
    with TestClient(app) as admin, TestClient(app) as outsider:
        assert (
            outsider.post(f"/api/admin/songs/{song_id}/backing/retry").status_code
            == 401
        )
        login(admin)
        assert (
            admin.put(
                "/api/admin/settings", json={"require_backing_ready": True}
            ).status_code
            == 200
        )
        assert admin.get("/api/state").json()["queue"] == []
        assert admin.get(f"/api/songs/{song_id}/detail").json()["awaiting_backing"]
        assert admin.post("/api/admin/songs/9999/backing/retry").status_code == 404
        retry = admin.post(f"/api/admin/songs/{song_id}/backing/retry")
        assert retry.status_code == 200, retry.text
        assert retry.json()["status"] == "pending"
        detail = admin.get(f"/api/songs/{song_id}/detail").json()
        assert detail["backing"]["status"] == "pending"
        assert detail["backing"]["elapsed_seconds"] is None
        assert detail["backing"]["error"] == ""
        assert (
            admin.post(f"/api/admin/songs/{song_id}/backing/retry").status_code == 409
        )
        complete_backing(app)
        assert [s["id"] for s in admin.get("/api/state").json()["queue"]] == [song_id]
        assert admin.get(f"/api/songs/{song_id}/detail").json()["playable"]


def test_disabled_generation_cannot_be_made_mandatory(tmp_path, monkeypatch):
    monkeypatch.setenv("VC_KTV_BACKING_TRACKS", "0")
    app = create_app(
        root=tmp_path / "disabled",
        password="pw",
        secret=b"disabled-backing-test",
        auto_prepare=False,
    )
    app.state.events.create("Room", "123456")
    with TestClient(app) as admin:
        login(admin)
        assert (
            admin.put(
                "/api/admin/settings", json={"require_backing_ready": True}
            ).status_code
            == 409
        )
        assert not admin.get("/api/admin/settings").json()["require_backing_ready"]


def test_preexisting_backing_db_is_migrated_without_losing_rows(tmp_path):
    event_dir = tmp_path / "legacy"
    event_dir.mkdir()
    with sqlite3.connect(event_dir / "room.sqlite3") as conn:
        conn.execute(
            """CREATE TABLE backing_tracks(
               cache_key TEXT PRIMARY KEY, source_file TEXT NOT NULL,
               model_sha256 TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
               error TEXT NOT NULL DEFAULT '',updated_at INTEGER NOT NULL DEFAULT 0)"""
        )
        conn.execute(
            "INSERT INTO backing_tracks(cache_key,source_file,model_sha256,status) "
            "VALUES('legacy','a.mp4','old','ready')"
        )
    store = Store(event_dir)
    try:
        row = store.db.execute(
            "SELECT status,elapsed_seconds FROM backing_tracks WHERE cache_key='legacy'"
        ).fetchone()
        assert row["status"] == "ready"
        assert row["elapsed_seconds"] is None
    finally:
        store.db.close()


def test_virtual_replay_detail_reuses_parent_backing_metrics(tmp_path, monkeypatch):
    app, original, key = setup_song(tmp_path, monkeypatch)
    replay = add_ready(app, replay_of=original)
    complete_backing(app)
    record(app, key, "ready", duration=61.5)
    with TestClient(app) as admin:
        login(admin)
        first = admin.get(f"/api/songs/{original}/detail").json()["backing"]
        second = admin.get(f"/api/songs/{replay}/detail").json()["backing"]
        assert first == second
        assert second["status"] == "ready"
        assert second["elapsed_seconds"] == 61.5


def test_enabled_but_stopped_backing_worker_cannot_force_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("VC_KTV_BACKING_TRACKS", "1")
    app = create_app(
        root=tmp_path / "stopped",
        password="pw",
        secret=b"stopped-backing-test",
        auto_prepare=False,
    )
    app.state.events.create("Room", "123456")
    with TestClient(app) as admin:
        login(admin)
        response = admin.put(
            "/api/admin/settings", json={"require_backing_ready": True}
        )
        assert response.status_code == 409
        assert not admin.get("/api/admin/settings").json()["require_backing_ready"]
