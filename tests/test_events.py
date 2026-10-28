import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import Store, create_app


def make_app(root):
    app = create_app(
        root=root, password="test-admin", secret=b"test-events-key", auto_prepare=False
    )

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "场次隔离测试歌曲",
            "uploader": "测试",
            "cover": "",
            "duration": 20,
        }

    app.state.lookup = lookup
    app.state.probe = lambda file: 20
    return app


def admin_login(client):
    assert (
        client.post("/api/admin/login", json={"password": "test-admin"}).status_code
        == 200
    )


def test_session_lifecycle_isolates_cookie_tv_media_and_song_files(tmp_path):
    root = tmp_path / "ktv"
    app = make_app(root)
    with TestClient(app) as admin, TestClient(app) as user:
        assert admin.get("/api/event").json()["event"] is None
        assert user.post("/api/songs", json={"bvid": "BV0000000001"}).status_code == 401
        assert user.post("/api/admin/events", json={"name": "非法"}).status_code == 401
        admin_login(admin)
        created = admin.post(
            "/api/admin/events", json={"name": "第一场", "code": "000012"}
        )
        assert created.status_code == 200, created.text
        first = created.json()
        assert first["code"] == "000012"
        assert first["id"] == app.state.events.active_id
        assert (root / "sessions" / first["id"] / "room.sqlite3").exists()
        assert admin.get("/api/event").json()["event"]["name"] == "第一场"
        assert admin.post("/api/admin/events", json={"name": "重复"}).status_code == 409

        join_and_register(user, "第一场用户", "000012")
        first_song = user.post("/api/songs", json={"bvid": "BV0000000001"})
        assert first_song.status_code == 200
        song_id = first_song.json()["id"]
        assert user.get("/api/state").json()["waiting"][0]["mine"]
        media = admin.post(
            f"/api/admin/songs/{song_id}/media",
            files={"file": ("video.mp4", b"valid-media", "video/mp4")},
        )
        assert media.status_code == 200, media.text
        token = admin.get("/api/admin/tv-link").json()["url"].split("token=")[1]
        active = admin.post("/api/admin/next").json()["active"]
        assert user.get(active["media_url"]).content == b"valid-media"
        assert admin.post("/api/admin/events/close").status_code == 409
        assert (
            admin.post("/api/admin/finish", json={"song_id": song_id}).status_code
            == 200
        )
        assert admin.post("/api/admin/events/close").status_code == 200

        assert user.get(active["media_url"]).status_code == 403
        assert user.get("/api/tv/state", params={"token": token}).status_code == 401
        assert (root / "sessions" / first["id"] / "media").is_dir()
        assert (root / "sessions" / first["id"] / "room.sqlite3").exists()
        assert not (root / "active-session").exists()

        second = admin.post("/api/admin/events", json={"name": "第二场"})
        assert second.status_code == 200, second.text
        assert second.json()["id"] != first["id"]
        assert len(second.json()["code"]) == 6
        assert user.get("/api/state").status_code == 401
        assert admin.get("/api/state").json()["queue"] == []
        assert admin.get("/api/state").json()["guests"] == []
        assert user.get("/api/tv/state", params={"token": token}).status_code == 401
        assert len(admin.get("/api/admin/events").json()["events"]) == 2

    reopened = make_app(root)
    assert reopened.state.events.active_id == second.json()["id"]
    assert len(reopened.state.events.list()) == 2


def test_existing_legacy_data_remains_intact_and_is_not_reactivated(tmp_path):
    root = tmp_path / "old-root"
    legacy = Store(root)
    legacy.db.execute("INSERT INTO guests(id,name) VALUES(?,?)", ("old-user", "旧场"))
    legacy.db.commit()
    legacy.db.close()
    app = make_app(root)
    assert app.state.events.active_id == "legacy"
    with TestClient(app) as admin:
        admin_login(admin)
        assert admin.get("/api/admin/events").json()["active"] == "legacy"
        assert admin.post("/api/admin/events/close").status_code == 200
        assert app.state.events.active_id is None
    restarted = make_app(root)
    assert restarted.state.events.active_id is None
    assert (root / "room.sqlite3").exists()
    assert (root / "legacy.closed").exists()
    assert restarted.state.events.list()[0]["status"] == "closed"


def test_offline_legacy_archive_is_one_directory(tmp_path):
    import importlib.util
    from pathlib import Path

    root = tmp_path / "old-root"
    old = Store(root)
    (old.media / "sample.mp4").write_bytes(b"old-media")
    old.db.execute("INSERT INTO guests(id,name) VALUES(?,?)", ("old", "旧用户"))
    old.db.commit()
    old.db.close()
    app = make_app(root)
    app.state.events.close()

    script = Path(__file__).resolve().parents[1] / "scripts/archive_legacy.py"
    spec = importlib.util.spec_from_file_location("vc_ktv_legacy_archive", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dest = module.archive(root)
    assert (dest / "room.sqlite3").is_file()
    assert (dest / "media/sample.mp4").read_bytes() == b"old-media"
    assert not (root / "room.sqlite3").exists()
    assert not (root / "media").exists()
    new_app = make_app(root)
    assert new_app.state.events.active_id is None
    assert new_app.state.events.list()[0]["id"] == "legacy"


def test_inflight_admin_poll_pins_event_during_close(tmp_path, monkeypatch):
    app = make_app(tmp_path / "room")
    entered = threading.Event()
    release = threading.Event()
    original = Store.state

    def paused_state(store):
        entered.set()
        assert release.wait(5)
        return original(store)

    with TestClient(app) as admin, TestClient(app) as poller:
        admin_login(admin)
        created = admin.post("/api/admin/events", json={"name": "Old"}).json()
        poller.cookies.update(admin.cookies)
        monkeypatch.setattr(Store, "state", paused_state)

        with ThreadPoolExecutor(max_workers=2) as executor:
            poll = executor.submit(poller.get, "/api/admin/state")
            try:
                assert entered.wait(5), "state route did not reach the barrier"
                closing = executor.submit(admin.post, "/api/admin/events/close")
                for _ in range(100):
                    if app.state.events.transitioning:
                        break
                    time.sleep(0.01)
                assert app.state.events.transitioning
                assert not closing.done()
                assert poller.get("/api/admin/state").status_code == 409
            finally:
                release.set()
            assert poll.result(timeout=5).status_code == 200
            assert closing.result(timeout=5).status_code == 200
        assert app.state.events.active_id is None
        assert admin.post("/api/admin/events", json={"name": "New"}).status_code == 200
        assert admin.get("/api/admin/state").json()["queue"] == []
        assert (tmp_path / "room/sessions" / created["id"] / "room.sqlite3").is_file()


def test_close_rejects_busy_worker_and_retires_idle_worker(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"test-events-key",
        auto_prepare=True,
    )
    app.state.events.create("Old", "123456")
    entered = threading.Event()
    release = threading.Event()

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "Test",
            "uploader": "tester",
            "cover": "",
            "duration": 2,
        }

    def slow_download(bvid, page, directory):
        entered.set()
        assert release.wait(5)
        path = directory / "source.mp4"
        path.write_bytes(b"fake-video")
        return path

    app.state.lookup = lookup
    app.state.autoprep.download = slow_download
    app.state.autoprep.probe = lambda path: 2
    with TestClient(app) as admin, TestClient(app) as guest:
        admin_login(admin)
        join_and_register(guest, "guest", "123456")
        assert (
            guest.post("/api/songs", json={"bvid": "BV0000000001"}).status_code == 200
        )
        try:
            assert entered.wait(5)
            assert admin.post("/api/admin/events/close").status_code == 409
        finally:
            release.set()
        worker = app.state.autoprep
        for _ in range(200):
            if not worker.busy.is_set():
                break
            time.sleep(0.01)
        assert not worker.busy.is_set()
        assert admin.post("/api/admin/events/close").status_code == 200
        assert not worker.worker.is_alive()
        assert admin.post("/api/admin/events", json={"name": "New"}).status_code == 200
        assert app.state.autoprep is not worker
        assert app.state.autoprep.store is app.state.events.current_store
        assert not worker.worker.is_alive()


def test_create_waits_for_request_that_started_without_event(tmp_path, monkeypatch):
    app = make_app(tmp_path / "room")
    entered = threading.Event()
    release = threading.Event()
    original = app.state.events.metadata

    def paused_metadata():
        entered.set()
        assert release.wait(5)
        return original()

    with TestClient(app) as admin, TestClient(app) as guest:
        admin_login(admin)
        monkeypatch.setattr(app.state.events, "metadata", paused_metadata)
        with ThreadPoolExecutor(max_workers=2) as executor:
            old = executor.submit(guest.get, "/api/event")
            try:
                assert entered.wait(5)
                created = executor.submit(
                    admin.post, "/api/admin/events", json={"name": "New"}
                )
                for _ in range(100):
                    if app.state.events.transitioning:
                        break
                    time.sleep(0.01)
                assert app.state.events.transitioning
                assert not created.done()
                assert guest.get("/api/event").status_code == 409
            finally:
                release.set()
            assert old.result(timeout=5).json()["event"] is None
            assert created.result(timeout=5).status_code == 200


def test_manual_render_worker_stays_with_own_event(tmp_path, monkeypatch):
    app = make_app(tmp_path / "room")
    entered = threading.Event()
    release = threading.Event()
    destinations = []

    def delayed_render(source, lyrics, destination, duration, lead):
        destinations.append(destination)
        entered.set()
        assert release.wait(5)
        destination.write_bytes(b"rendered")

    monkeypatch.setattr("vc_ktv.app.render_video", delayed_render)
    with TestClient(app) as admin, TestClient(app) as guest:
        admin_login(admin)
        old = admin.post("/api/admin/events", json={"name": "Old"}).json()
        join_and_register(guest, "guest", old["code"])
        song = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        assert (
            admin.post(
                f"/api/admin/songs/{song}/media",
                files={"file": ("source.mp4", b"source", "video/mp4")},
            ).status_code
            == 200
        )
        assert (
            admin.post(
                f"/api/admin/songs/{song}/lyrics",
                json={"text": "[00:01.00]测试歌曲"},
            ).status_code
            == 200
        )
        assert (
            admin.post(
                f"/api/admin/songs/{song}/render", json={"lead": 1.0}
            ).status_code
            == 200
        )
        try:
            assert entered.wait(5)
            assert admin.post("/api/admin/events/close").status_code == 409
        finally:
            release.set()
        for _ in range(200):
            if destinations and destinations[0].is_file():
                break
            time.sleep(0.01)
        assert destinations[0].is_file()
        old_media = app.state.events.current_store.media
        assert destinations[0].parent == old_media
        # Read completion under Store's lock, not solely by file existence.
        for _ in range(200):
            if (
                not app.state.store.rows(
                    "SELECT status FROM songs WHERE id=?", (song,)
                )[0]["status"]
                == "rendering"
            ):
                break
            time.sleep(0.01)
        assert admin.post("/api/admin/events/close").status_code == 200
        assert admin.post("/api/admin/events", json={"name": "New"}).status_code == 200
        assert not list(app.state.events.current_store.media.iterdir())
        assert destinations[0].read_bytes() == b"rendered"
