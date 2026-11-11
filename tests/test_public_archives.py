"""Public archive visibility, event-scoped details, and administrative corrections."""

import sqlite3

from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app


def test_public_archives_and_status_editing_are_event_scoped(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"archive-tests",
        auto_prepare=False,
    )
    with TestClient(app) as admin, TestClient(app) as visitor:
        assert (
            admin.post("/api/admin/login", json={"password": "admin"}).status_code
            == 200
        )
        first = admin.post(
            "/api/admin/events", json={"name": "首场", "code": "123456"}
        ).json()
        store = app.state.events.active()
        with store.lock:
            store.db.execute("INSERT INTO guests (id,name) VALUES ('g1','客人')")
            store.db.execute(
                """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,
                   status,prep_state,played_at) VALUES ('BV0000000001',2,'旧歌','UP','',70,'g1',1,
                   'played','done',10)"""
            )
            store.db.commit()
        assert admin.post("/api/admin/events/close").status_code == 200
        public = f"/api/events/{first['id']}"
        assert visitor.get("/api/events").json()["events"] == []
        assert visitor.get(public + "/songs").status_code == 404
        assert visitor.get(public + "/songs/1").status_code == 404
        assert visitor.get(public + "/songs/1/download").status_code == 404
        assert visitor.get("/api/songs/1/download").status_code == 404
        assert admin.get(public + "/songs/1").json()["song"]["title"] == "旧歌"
        assert (
            admin.put(
                f"/api/admin/events/{first['id']}/visibility?public=true"
            ).status_code
            == 200
        )
        events = visitor.get("/api/events").json()["events"]
        assert len(events) == 1 and events[0]["name"] == "首场"
        assert "code" not in events[0]
        public_songs = visitor.get(public + "/songs").json()
        assert public_songs["songs"][0]["id"] == 1
        song = visitor.get(public + "/songs/1").json()["song"]
        assert (song["bvid"], song["page"], song["status"]) == (
            "BV0000000001",
            2,
            "played",
        )
        assert not any("media" in field or "download" in field for field in song)
        assert visitor.get(public + "/songs/2").status_code == 404
        # A second session can have song id 1 without confusing the archive identity.
        second = admin.post(
            "/api/admin/events", json={"name": "次场", "code": "654321"}
        ).json()
        current = app.state.events.active()
        with current.lock:
            current.db.execute("INSERT INTO guests (id,name) VALUES ('g2','新人')")
            current.db.execute(
                """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,
                   status,prep_state) VALUES ('BV0000000002',1,'新歌','UP','',60,'g2',1,
                   'waiting','pending')"""
            )
            current.db.commit()
        assert app.state.events.active_id == second["id"]
        assert visitor.get(public + "/songs/1").json()["song"]["title"] == "旧歌"
        assert admin.post("/api/admin/events/close").status_code == 200
        assert (
            admin.post(f"/api/admin/events/{first['id']}/release-media").status_code
            == 200
        )
        assert (
            admin.put(
                f"/api/admin/events/{first['id']}/songs/1/status?status=closed_unplayed"
            ).status_code
            == 200
        )
        assert (
            visitor.get(public + "/songs/1").json()["song"]["status"]
            == "closed_unplayed"
        )
        csv = admin.get(f"/api/admin/events/{first['id']}/songs.csv")
        assert csv.status_code == 200 and "closed_unplayed" in csv.text
        with sqlite3.connect(
            tmp_path / "room" / "sessions" / first["id"] / "room.sqlite3"
        ) as db:
            assert (
                db.execute("SELECT archive_order FROM songs WHERE id=1").fetchone()[0]
                == 1
            )
        assert (
            "closed_unplayed"
            in (tmp_path / "room" / "sessions" / first["id"] / "songs.csv").read_text()
        )
        assert (
            admin.put(
                f"/api/admin/events/{first['id']}/visibility?public=false"
            ).status_code
            == 200
        )
        assert visitor.get(public + "/songs/1").status_code == 404
        assert visitor.get(public + "/songs").status_code == 404
        assert visitor.get("/api/events").json()["events"] == []
        assert admin.get(public + "/songs/1").status_code == 200
        assert admin.delete(f"/api/admin/events/{first['id']}").status_code == 200
        assert admin.get(public + "/songs/1").status_code == 404
