from fastapi.testclient import TestClient

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
        assert user.post("/api/songs", json={"bvid": "BV0000000001"}).status_code == 409
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
