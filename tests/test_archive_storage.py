"""Archive cleanup is scoped to closed normal sessions and global admins."""
import csv
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_event_admins import create_account, event_login
from tests.test_events import admin_login, make_app


def test_closed_event_release_media_and_delete_record(tmp_path):
    root = tmp_path / "ktv"
    app = make_app(root)
    with (
        TestClient(app) as admin,
        TestClient(app) as guest,
        TestClient(app) as operator,
    ):
        admin_login(admin)
        first = admin.post(
            "/api/admin/events", json={"name": "初场", "code": "000012"}
        ).json()
        join_and_register(guest, "点歌者", "000012")
        store = app.state.events.active()
        guest_id = store.db.execute("SELECT id FROM guests LIMIT 1").fetchone()[0]
        with store.lock:
            store.db.executemany(
                """INSERT INTO songs (
                    bvid,page,title,uploader,cover,duration,requester,
                    requested_at,status,source_type,source_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    ("BV0000000001", 1, "B站歌", "UP", "", 100, guest_id, 1, "played", "bilibili", ""),
                    ("", 1, "网易云歌", "歌手", "", 90, guest_id, 2, "played", "netease", "2613869782"),
                ],
            )
            store.db.commit()
        first_dir = root / "sessions" / first["id"]
        (first_dir / "media" / "a.mp4").write_bytes(b"x" * 500)
        work = first_dir / "media" / "lyric-alignment"
        work.mkdir()
        (work / "word.lrc").write_text("[00:00.00]歌词")
        (work / "large.json").write_bytes(b"x" * (1024 * 1024 + 1))
        external = tmp_path / "outside.mp4"
        external.write_bytes(b"never delete this")
        (work / "external.mp4").symlink_to(external)

        assert admin.post("/api/admin/events/close").status_code == 200
        second = admin.post("/api/admin/events", json={"name": "后场"}).json()
        second_dir = root / "sessions" / second["id"]
        (second_dir / "media" / "active.mp4").write_bytes(b"active")
        create_account(admin)
        assert event_login(operator).status_code == 200

        release = f"/api/admin/events/{first['id']}/release-media"
        delete = f"/api/admin/events/{first['id']}"
        assert guest.post(release).status_code == 401
        assert guest.delete(delete).status_code == 401
        assert operator.post(release).status_code == 403
        assert operator.delete(delete).status_code == 403
        assert admin.post(f"/api/admin/events/{second['id']}/release-media").status_code == 409
        assert admin.delete(f"/api/admin/events/{second['id']}").status_code == 409
        assert admin.post("/api/admin/events/legacy/release-media").status_code == 409
        assert admin.delete("/api/admin/events/legacy").status_code == 409

        freed = admin.post(release)
        assert freed.status_code == 200, freed.text
        assert freed.json()["freed_bytes"] == 500 + 1024 * 1024 + 1
        assert not (first_dir / "media" / "a.mp4").exists()
        assert not (work / "large.json").exists()
        assert not (work / "external.mp4").is_symlink()
        assert external.read_bytes() == b"never delete this"
        assert (work / "word.lrc").read_text() == "[00:00.00]歌词"
        assert (first_dir / "info.json").is_file()
        assert (first_dir / "room.sqlite3").is_file()
        csv_bytes = (first_dir / "songs.csv").read_bytes()
        assert csv_bytes.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        assert [row["source_type"] for row in rows] == ["bilibili", "netease"]
        assert rows[1]["source_id"] == "2613869782"
        assert admin.get(f"{delete}/songs.csv").content == csv_bytes
        assert (second_dir / "media" / "active.mp4").read_bytes() == b"active"
        status = next(e for e in admin.get("/api/admin/events").json()["events"]
                      if e["id"] == first["id"])
        assert status.get("media_released_at")

        again = admin.post(release)
        assert again.status_code == 200
        assert again.json()["freed_bytes"] == 0
        assert again.json()["media_released_at"] == status["media_released_at"]

        assert admin.delete(delete).status_code == 200
        assert not first_dir.exists()
        assert admin.delete(delete).status_code == 404
        assert admin.get(f"{delete}/songs.csv").status_code == 404
        assert second_dir.is_dir()
        assert (second_dir / "media" / "active.mp4").read_bytes() == b"active"
        assert all(e["id"] != first["id"]
                   for e in admin.get("/api/admin/events").json()["events"])


def test_archive_cleanup_rejects_symlinked_session_directory(tmp_path):
    root = tmp_path / "room"
    app = make_app(root)
    with TestClient(app) as admin:
        admin_login(admin)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "info.json").write_text(json.dumps({
            "id": "symlink-event", "name": "Bad", "status": "closed",
        }))
        (root / "sessions" / "symlink-event").symlink_to(outside, target_is_directory=True)
        assert admin.post("/api/admin/events/symlink-event/release-media").status_code == 404
        assert admin.delete("/api/admin/events/symlink-event").status_code == 404
        assert (outside / "info.json").exists()
