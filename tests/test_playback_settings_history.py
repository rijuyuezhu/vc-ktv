"""Admin settings and isolated per-event song history export."""

import csv
import io
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from vc_ktv.app import Store, create_app


def app_for_policy(tmp_path):
    app = create_app(
        root=tmp_path / "ktv",
        password="test-admin",
        secret=b"policy-test-secret",
        auto_prepare=False,
    )
    return app


def admin_login(client):
    assert (
        client.post("/api/admin/login", json={"password": "test-admin"}).status_code
        == 200
    )


def add_song(store, requester, requested_at, prep_state="manual", page=1, title="歌"):
    with store.lock:
        store.db.execute(
            "INSERT OR IGNORE INTO guests(id,name) VALUES (?,?)", (requester, requester)
        )
        row = store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,"
            "requested_at,status,media_file,source_file,prep_state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "BV0000000001",
                page,
                title,
                "测试UP",
                "",
                150,
                requester,
                requested_at,
                "ready",
                "existing.mp4",
                "existing.mp4",
                prep_state,
            ),
        )
        store.db.commit()
        return row.lastrowid


def test_defaults_subtitle_gate_and_manual_media(tmp_path):
    app = app_for_policy(tmp_path)
    event = app.state.events.create("测试", "000001")
    assert event["id"]
    with TestClient(app) as admin, TestClient(app) as guest:
        assert guest.get("/api/admin/settings").status_code == 401
        assert (
            guest.put(
                "/api/admin/settings",
                json={"queue_policy": "fcfs", "allow_unsubtitled": True},
            ).status_code
            == 401
        )
        admin_login(admin)
        assert admin.get("/api/admin/settings").json() == {
            "queue_policy": "fair",
            "allow_unsubtitled": False,
            "subtitle_style": "classic",
            "video_quality": 720,
            "compact_video": False,
            "orders_paused": False,
        }
        store = app.state.store
        original = add_song(store, "A", 1, "downloaded")
        done = add_song(store, "B", 2, "done")
        manual = add_song(store, "C", 3, "manual")
        failed = add_song(store, "D", 4, "original")
        first = admin.get("/api/state").json()
        assert {s["id"] for s in first["queue"]} == {done, manual}
        assert {s["id"] for s in first["waiting"]} == {original, failed}
        assert all(s["awaiting_subtitles"] for s in first["waiting"])
        assert admin.post("/api/admin/next").json()["active"]["id"] == done
        assert admin.post("/api/admin/next").json()["active"]["id"] == done
        assert (
            admin.put(
                "/api/admin/settings",
                json={"queue_policy": "fcfs", "allow_unsubtitled": True},
            ).status_code
            == 200
        )
        assert admin.post("/api/admin/next").json()["active"]["id"] == done
        assert (
            admin.post(
                "/api/admin/finish", json={"song_id": done, "reason": "skip"}
            ).status_code
            == 200
        )
        assert admin.post("/api/admin/next").json()["active"]["id"] == original
        assert admin.get("/api/admin/settings").json()["allow_unsubtitled"] is True
        assert (
            admin.put(
                "/api/admin/settings",
                json={"queue_policy": "round_robin", "allow_unsubtitled": True},
            ).status_code
            == 422
        )
        assert (
            admin.put(
                "/api/admin/settings",
                json={"queue_policy": "fcfs", "allow_unsubtitled": "maybe"},
            ).status_code
            == 422
        )


def test_fairness_vs_fcfs_priority_and_counter(tmp_path):
    app = app_for_policy(tmp_path)
    app.state.events.create("公平", "654321")
    with TestClient(app) as admin:
        admin_login(admin)
        store = app.state.store
        a1 = add_song(store, "A", 100)
        a2 = add_song(store, "A", 101)
        b1 = add_song(store, "B", 102)
        with store.lock:
            store.db.execute("UPDATE guests SET plays=2 WHERE id='A'")
            store.db.execute(
                "INSERT INTO guests(id,name,plays) VALUES ('C','No songs',0)"
            )
            store.db.commit()
        assert [s["id"] for s in admin.get("/api/state").json()["queue"]] == [
            b1,
            a1,
            a2,
        ]
        assert admin.post("/api/admin/next").json()["active"]["id"] == b1
        assert admin.post("/api/admin/finish", json={"song_id": b1}).status_code == 200
        assert (
            store.db.execute("SELECT plays FROM guests WHERE id='B'").fetchone()[0] == 1
        )
        assert (
            admin.put(
                "/api/admin/settings",
                json={"queue_policy": "fcfs", "allow_unsubtitled": False},
            ).status_code
            == 200
        )
        assert [s["id"] for s in admin.get("/api/state").json()["queue"]] == [a1, a2]
        assert admin.post("/api/admin/next").json()["active"]["id"] == a1
        assert (
            admin.post(
                "/api/admin/finish", json={"song_id": a1, "reason": "skip"}
            ).status_code
            == 200
        )
        assert (
            store.db.execute("SELECT plays FROM guests WHERE id='A'").fetchone()[0] == 2
        )


def test_settings_are_per_event_and_survive_reopen(tmp_path):
    root = tmp_path / "ktv"
    app = app_for_policy(tmp_path)
    first = app.state.events.create("第一场", "000001")
    with TestClient(app) as admin:
        admin_login(admin)
        admin.put(
            "/api/admin/settings",
            json={
                "queue_policy": "fcfs",
                "allow_unsubtitled": True,
                "subtitle_style": "large",
            },
        )
        assert admin.get("/api/admin/settings").json()["subtitle_style"] == "large"
        # Legacy settings clients do not reset the configured subtitle style.
        admin.put(
            "/api/admin/settings",
            json={"queue_policy": "fair", "allow_unsubtitled": True},
        )
        assert admin.get("/api/admin/settings").json()["subtitle_style"] == "large"
        assert admin.post("/api/admin/events/close").status_code == 200
        second = admin.post(
            "/api/admin/events", json={"name": "第二场", "code": "000002"}
        )
        assert second.status_code == 200
        assert admin.get("/api/admin/settings").json() == {
            "queue_policy": "fair",
            "allow_unsubtitled": False,
            "subtitle_style": "classic",
            "video_quality": 720,
            "compact_video": False,
            "orders_paused": False,
        }
    reopened = create_app(
        root=root,
        password="test-admin",
        secret=b"policy-test-secret",
        auto_prepare=False,
    )
    assert reopened.state.events.active_id == second.json()["id"]
    assert reopened.state.store.settings() == {
        "queue_policy": "fair",
        "allow_unsubtitled": False,
        "subtitle_style": "classic",
        "video_quality": 720,
        "compact_video": False,
        "orders_paused": False,
    }
    with sqlite3.connect(root / "sessions" / first["id"] / "room.sqlite3") as db:
        assert dict(
            db.execute(
                "SELECT name,value FROM room_state WHERE name IN "
                "('queue_policy','allow_unsubtitled')"
            )
        ) == {"queue_policy": "fair", "allow_unsubtitled": "1"}


def test_export_archives_unicode_repeats_and_legacy(tmp_path):
    app = app_for_policy(tmp_path)
    first = app.state.events.create("历史", "000001")
    with TestClient(app) as admin, TestClient(app) as guest:
        assert (
            guest.get(f"/api/admin/events/{first['id']}/songs.csv").status_code == 401
        )
        admin_login(admin)
        add_song(app.state.store, "Alice", 100, page=2, title="中文歌曲")
        add_song(app.state.store, "Alice", 101, page=1, title="=HYPERLINK(1)")
        assert admin.post("/api/admin/events/close").status_code == 200
        assert (
            admin.post("/api/admin/events", json={"name": "下一场"}).status_code == 200
        )
        url = f"/api/admin/events/{first['id']}/songs.csv"
        exported = admin.get(url)
        assert exported.status_code == 200, exported.text
        assert "attachment" in exported.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert [r["order"] for r in rows] == ["1", "2"]
        assert [r["page"] for r in rows] == ["2", "1"]
        assert [r["bvid"] for r in rows] == ["BV0000000001"] * 2
        assert rows[0]["title"] == "中文歌曲"
        assert rows[1]["title"].startswith("'=")
        assert admin.get("/api/admin/events/../songs.csv").status_code != 200
        assert admin.get("/api/admin/events/not_a_session/songs.csv").status_code == 404
        assert (
            admin.get(
                f"/api/admin/events/{app.state.events.active_id}/songs.csv"
            ).status_code
            == 200
        )

    oldroot = tmp_path / "legacy"
    legacy = Store(oldroot)
    add_song(legacy, "old", 2, page=3)
    legacy.db.close()
    oldapp = create_app(
        root=oldroot, password="test-admin", secret=b"legacy-key", auto_prepare=False
    )
    with TestClient(oldapp) as admin:
        admin_login(admin)
        resp = admin.get("/api/admin/events/legacy/songs.csv")
        assert resp.status_code == 200
        assert "BV0000000001" in resp.text
        assert admin.post("/api/admin/events/close").status_code == 200
        assert admin.get("/api/admin/events/legacy/songs.csv").status_code == 200
        # Legacy data moved into sessions/legacy is still available.
        import importlib.util

        script = Path(__file__).resolve().parents[1] / "scripts/archive_legacy.py"
        spec = importlib.util.spec_from_file_location(
            "legacy_archive_for_export", script
        )
        archive_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(archive_module)
        archive_module.archive(oldroot)
        exported = admin.get("/api/admin/events/legacy/songs.csv")
        assert exported.status_code == 200
        assert "BV0000000001" in exported.text


def test_video_quality_setting_is_admin_only_persistent_and_per_event(tmp_path):
    app = app_for_policy(tmp_path)
    app.state.events.create("first", "000001")
    with TestClient(app) as admin, TestClient(app) as guest:
        admin_login(admin)
        assert admin.get("/api/admin/settings").json()["video_quality"] == 720
        assert (
            guest.put(
                "/api/admin/settings",
                json={
                    "queue_policy": "fair",
                    "allow_unsubtitled": False,
                    "video_quality": 480,
                },
            ).status_code
            == 401
        )
        assert (
            admin.put(
                "/api/admin/settings",
                json={
                    "queue_policy": "fair",
                    "allow_unsubtitled": False,
                    "video_quality": 1080,
                },
            ).status_code
            == 422
        )
        result = admin.put(
            "/api/admin/settings",
            json={
                "queue_policy": "fair",
                "allow_unsubtitled": False,
                "video_quality": 480,
            },
        )
        assert result.status_code == 200
        assert result.json()["video_quality"] == 480
        # Old clients updating unrelated settings must not reset quality.
        assert (
            admin.put(
                "/api/admin/settings",
                json={
                    "queue_policy": "fcfs",
                    "allow_unsubtitled": True,
                },
            ).status_code
            == 200
        )
        assert admin.get("/api/admin/settings").json()["video_quality"] == 480
        assert admin.post("/api/admin/events/close").status_code == 200
        assert (
            admin.post(
                "/api/admin/events",
                json={
                    "name": "next",
                    "code": "000002",
                },
            ).status_code
            == 200
        )
        assert admin.get("/api/admin/settings").json()["video_quality"] == 720
