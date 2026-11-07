"""Closure freezes the actual/fair order and settles every unfinished song."""

import csv
import io
import sqlite3
import subprocess
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_events import admin_login, make_app
from vc_ktv.application.factory import create_app


def test_close_preserves_turn_order_and_marks_unfinished(tmp_path):
    root = tmp_path / "room"
    app = make_app(root)
    with TestClient(app) as admin:
        admin_login(admin)
        room = admin.post("/api/admin/events", json={"name": "本场"}).json()
        store = app.state.events.active()
        with store.lock:
            store.db.executemany(
                "INSERT INTO guests(id,name,plays) VALUES (?,?,?)",
                [("a", "甲", 2), ("b", "乙", 0)],
            )
            rows = [
                (1, "a", "ready", None, 1),
                (2, "a", "played", 400, 2),
                (3, "b", "cancelled", None, 3),
                (4, "b", "waiting", None, 4),
                (5, "a", "playing", None, 5),
                (6, "a", "skipped", 500, 6),
                (7, "b", "ready", None, 7),
                (8, "b", "rendering", None, 8),
            ]
            for sid, requester, state, played, requested in rows:
                store.db.execute(
                    """INSERT INTO songs (
                        id,bvid,page,title,uploader,cover,duration,requester,
                        requested_at,status,prep_state,media_file,played_at
                    ) VALUES(?, ?,1,?,'UP','',140,?,?,?,?,?,?)""",
                    (
                        sid,
                        "BV0000000001",
                        f"歌{sid}",
                        requester,
                        requested,
                        state,
                        "pending" if state == "waiting" else "done",
                        "media.mp4" if state in ("ready", "playing") else None,
                        played,
                    ),
                )
            store.db.commit()
            expected = store.close_song_order()
            assert expected[:3] == [2, 6, 5]
            assert expected[-1] == 3
            assert sorted(expected) == list(range(1, 9))

        path = f"/api/admin/events/{room['id']}/songs.csv"
        active = admin.get(path)
        assert active.status_code == 200
        active_rows = list(csv.DictReader(io.StringIO(active.text.lstrip("\ufeff"))))
        assert [int(row["title"][1:]) for row in active_rows] == expected
        assert list(active_rows[0]) == [
            "order",
            "title",
            "requester",
            "status",
            "source_type",
            "source_id",
            "bvid",
            "page",
            "requested_at",
            "played_at",
        ]
        assert admin.post("/api/admin/events/close").status_code == 200
        with sqlite3.connect(root / "sessions" / room["id"] / "room.sqlite3") as db:
            result = db.execute(
                "SELECT id,status,played_at,archive_order FROM songs ORDER BY archive_order"
            ).fetchall()
        assert [r[0] for r in result] == expected
        statuses = {row[0]: row[1] for row in result}
        assert statuses == {
            1: "closed_unplayed",
            2: "played",
            3: "cancelled",
            4: "closed_unplayed",
            5: "closed_interrupted",
            6: "skipped",
            7: "closed_unplayed",
            8: "closed_unplayed",
        }
        assert next(row for row in result if row[0] == 5)[2] is None
        archived = admin.get(path)
        assert archived.status_code == 200
        rows_after = list(csv.DictReader(io.StringIO(archived.text.lstrip("\ufeff"))))
        assert [int(row["title"][1:]) for row in rows_after] == expected
        assert [row["status"] for row in rows_after] == [
            statuses[sid] for sid in expected
        ]


def test_close_kills_active_download_process(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"stop-child-tree",
        auto_prepare=True,
    )
    app.state.events.create("本场", "123456")
    ready = threading.Event()
    processes = []

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "测试歌",
            "uploader": "UP",
            "cover": "",
            "duration": 30,
        }

    def download(bvid, page, directory):
        process = subprocess.Popen(["sleep", "30"])
        processes.append(process)
        ready.set()
        if process.wait() != 0:
            raise RuntimeError("download cancelled")
        raise AssertionError("download should have been stopped")

    app.state.lookup = lookup
    app.state.autoprep.download = download
    with TestClient(app) as admin, TestClient(app) as guest:
        admin_login(admin)
        join_and_register(guest, "点歌者", "123456")
        song = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        assert ready.wait(5)
        response = admin.post("/api/admin/events/close")
        assert response.status_code == 200, response.text
        assert not app.state.autoprep.worker.is_alive()
        assert not list((tmp_path / "room/sessions").glob("*/vc-ktv-*-*"))
        assert processes[0].poll() is not None
        assert app.state.events.active_id is None
        with sqlite3.connect(
            next((tmp_path / "room/sessions").glob("*/room.sqlite3"))
        ) as db:
            assert (
                db.execute("SELECT status FROM songs WHERE id=?", (song,)).fetchone()[0]
                == "closed_unplayed"
            )


def test_guest_and_admin_can_read_closed_history_without_other_room_access(tmp_path):
    root = tmp_path / "ktv"
    app = make_app(root)
    with (
        TestClient(app) as admin,
        TestClient(app) as guest,
        TestClient(app) as outsider,
    ):
        admin_login(admin)
        room = admin.post(
            "/api/admin/events", json={"name": "周末场", "code": "123456"}
        ).json()
        join_and_register(guest, "甲", "123456")
        store = app.state.events.active()
        uid = store.db.execute("SELECT id FROM guests LIMIT 1").fetchone()[0]
        with store.lock:
            for id, status in (
                (1, "played"),
                (2, "cancelled"),
                (3, "ready"),
                (4, "playing"),
            ):
                store.db.execute(
                    """INSERT INTO songs
                       (id,bvid,page,title,uploader,cover,duration,requester,
                        requested_at,status,prep_state,media_file,played_at)
                       VALUES(?, 'BV0000000001', 1, ?, 'UP','',90,?,?,?,?,?,?)""",
                    (
                        id,
                        f"歌{id}",
                        uid,
                        id,
                        status,
                        "done",
                        "song.mp4" if status in ("ready", "playing") else None,
                        10 if status == "played" else None,
                    ),
                )
            store.db.commit()
        assert outsider.get("/api/songs/history").status_code == 401
        live = guest.get("/api/songs/history")
        assert live.status_code == 200
        assert live.json()["archived"] is False
        assert [s["id"] for s in live.json()["songs"]] == [1, 4, 3, 2]
        assert admin.post("/api/admin/events/close").status_code == 200
        assert guest.get("/api/entry").json()["archived_event"]["id"] == room["id"]
        # Archives created before frozen ordering still have a useful order.
        with sqlite3.connect(root / "sessions" / room["id"] / "room.sqlite3") as db:
            db.execute("UPDATE songs SET archive_order=NULL")
        assert [
            row["id"] for row in guest.get("/api/songs/history").json()["songs"]
        ] == [1, 4, 3, 2]
        assert [
            row["id"]
            for row in admin.get(f"/api/admin/events/{room['id']}/songs").json()[
                "songs"
            ]
        ] == [1, 4, 3, 2]
        archived = guest.get("/api/songs/history")
        assert archived.status_code == 200
        assert archived.json()["archived"] is True
        assert [s["id"] for s in archived.json()["songs"]] == [1, 4, 3, 2]
        assert [s["status"] for s in archived.json()["songs"]] == [
            "played",
            "closed_interrupted",
            "closed_unplayed",
            "cancelled",
        ]
        assert outsider.get("/api/songs/history").status_code == 401
        records = admin.get(f"/api/admin/events/{room['id']}/songs")
        assert [s["id"] for s in records.json()["songs"]] == [1, 4, 3, 2]
        assert (
            admin.post("/api/admin/events", json={"name": "下一场"}).status_code == 200
        )
        assert guest.get("/api/songs/history").json()["archived"] is True
        assert guest.get("/api/entry").json()["archived_event"]["id"] == room["id"]


def test_failed_close_restores_songs_and_fair_order(tmp_path, monkeypatch):
    root = tmp_path / "room"
    app = make_app(root)
    with TestClient(app, raise_server_exceptions=False) as admin:
        admin_login(admin)
        room = admin.post("/api/admin/events", json={"name": "本场"}).json()
        store = app.state.events.active()
        with store.lock:
            store.db.execute("INSERT INTO guests(id,name) VALUES ('guest','甲')")
            store.db.execute(
                """INSERT INTO songs (
                    bvid,page,title,uploader,cover,duration,requester,
                    requested_at,status,prep_state,media_file
                ) VALUES('BV0000000001',1,'测试','UP','',45,'guest',1,
                         'playing','done','song.mp4')"""
            )
            store.db.commit()
        original_unlink = Path.unlink

        def fail_pointer(path, *args, **kwargs):
            if path == app.state.events.pointer:
                raise PermissionError("simulated pointer failure")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_pointer)
            assert admin.post("/api/admin/events/close").status_code == 500
        assert app.state.events.active_id == room["id"]
        row = store.db.execute(
            "SELECT status,archive_order,played_at FROM songs LIMIT 1"
        ).fetchone()
        assert tuple(row) == ("playing", None, None)
        assert app.state.events.metadata()["status"] == "active"
        assert admin.post("/api/admin/events/close").status_code == 200
        with sqlite3.connect(root / "sessions" / room["id"] / "room.sqlite3") as db:
            assert db.execute(
                "SELECT status,archive_order FROM songs LIMIT 1"
            ).fetchone() == ("closed_interrupted", 1)
