"""The optional accompaniment gate is enforced by the shared song scheduler."""

from fastapi.testclient import TestClient

from tests.helpers import primary_tv_token
from vc_ktv.application.factory import create_app
from vc_ktv.processing.backing_audio import KARAOKE_SHA256, source_identity


def event(tmp_path, monkeypatch):
    monkeypatch.setenv("VC_KTV_BACKING_TRACKS", "1")
    app = create_app(
        root=tmp_path / "event",
        password="pw",
        secret=b"test-backing-gate",
        auto_prepare=False,
    )
    app.state.events.create("Test", "123456")

    # Scheduler tests bypass the actual GPU worker but retain the admin contract.
    class Running:
        def is_alive(self):
            return True

    app.state.autoprep.backing.thread = Running()
    return app


def add_ready(app, *, user="person", name="video.mp4", replay_of=None):
    store = app.state.store
    with store.lock:
        store.db.execute(
            "INSERT OR IGNORE INTO guests(id,name) VALUES(?,?)", (user, user)
        )
        if replay_of is None:
            (store.media / name).write_bytes(b"synthetic-source" * 140)
        id_ = store.db.execute(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,
                   requested_at,status,prep_state,source_file,media_file,replay_of)
               VALUES('BV0000000001',1,'Ready','UP','',120,?,1,'ready','done',?,?,?)""",
            (
                user,
                name if replay_of is None else None,
                name if replay_of is None else None,
                replay_of,
            ),
        ).lastrowid
        store.db.commit()
        return id_


def complete_backing(app, name="video.mp4"):
    store = app.state.store
    source = store.media / name
    key = source_identity(source, KARAOKE_SHA256)
    folder = store.root / "backing"
    folder.mkdir(exist_ok=True)
    (folder / f"{key}.m4a").write_bytes(b"ready-aac" * 220)
    with store.lock:
        store.db.execute(
            """INSERT INTO backing_tracks(cache_key,source_file,model_sha256,status)
               VALUES(?,?,?,'ready')
               ON CONFLICT(cache_key) DO UPDATE SET status='ready'""",
            (key, name, KARAOKE_SHA256),
        )
        store.db.commit()
    return key


def test_admin_setting_default_off_and_tv_waits_until_backing_ready(
    tmp_path, monkeypatch
):
    app = event(tmp_path, monkeypatch)
    id_ = add_ready(app)
    with TestClient(app) as admin, TestClient(app) as tv:
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        assert admin.get("/api/admin/settings").json()["require_backing_ready"] is False
        assert [s["id"] for s in admin.get("/api/state").json()["queue"]] == [id_]
        assert (
            admin.put(
                "/api/admin/settings", json={"require_backing_ready": True}
            ).status_code
            == 200
        )
        assert admin.get("/api/admin/settings").json()["require_backing_ready"] is True
        blocked = admin.get("/api/state").json()
        assert blocked["queue"] == []
        assert blocked["waiting"][0]["awaiting_backing"] is True
        assert blocked["waiting"][0]["awaiting_subtitles"] is False
        token = primary_tv_token(admin)
        next_ = tv.post("/api/tv/next", params={"token": token})
        assert next_.status_code == 200 and next_.json()["active"] is None
        assert app.state.store.current() is None
        complete_backing(app)
        ready = admin.get("/api/state").json()
        assert [s["id"] for s in ready["queue"]] == [id_]
        assert ready["waiting"] == []
        next_ = tv.post("/api/tv/next", params={"token": token})
        assert next_.status_code == 200 and next_.json()["active"]["id"] == id_


def test_broken_backing_stays_waiting_and_admin_can_disable_requirement(
    tmp_path, monkeypatch
):
    app = event(tmp_path, monkeypatch)
    first = add_ready(app)
    second = add_ready(app, name="second.mp4")
    key = complete_backing(app)
    (app.state.store.root / "backing" / f"{key}.m4a").unlink()
    with TestClient(app) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        admin.put("/api/admin/settings", json={"require_backing_ready": True})
        state = admin.get("/api/state").json()
        assert state["queue"] == []
        assert {s["id"] for s in state["waiting"]} == {first, second}
        assert all(s["awaiting_backing"] for s in state["waiting"])
        # A different form must not silently clear the checkbox.
        admin.put("/api/admin/settings", json={"video_quality": 480})
        assert admin.get("/api/admin/settings").json()["require_backing_ready"] is True
        admin.put("/api/admin/settings", json={"require_backing_ready": False})
        assert {s["id"] for s in admin.get("/api/state").json()["queue"]} == {
            first,
            second,
        }


def test_shared_replay_source_reuses_ready_backing(tmp_path, monkeypatch):
    app = event(tmp_path, monkeypatch)
    original = add_ready(app)
    replay = add_ready(app, replay_of=original)
    complete_backing(app)
    store = app.state.store
    store.save_settings(None, None, require_backing_ready=True)
    with store.lock:
        ids = {s["id"] for s in store.fair_queue()}
        assert ids == {original, replay}
        assert store.backing_ready(store.songs()[0])
        assert store.backing_ready(store.songs()[1])


def test_requirement_is_stored_per_event_and_defaults_off_on_new_event(
    tmp_path, monkeypatch
):
    app = event(tmp_path, monkeypatch)
    app.state.store.save_settings(None, None, require_backing_ready=True)
    assert app.state.store.settings()["require_backing_ready"] is True
    first = app.state.events.active_id
    app.state.events.close()
    app.state.events.create("Second", "654321")
    assert app.state.events.active_id != first
    assert app.state.store.settings()["require_backing_ready"] is False
    reopened = create_app(
        root=tmp_path / "event",
        password="pw",
        secret=b"test-backing-gate",
        auto_prepare=False,
    )
    assert reopened.state.store.settings()["require_backing_ready"] is False
