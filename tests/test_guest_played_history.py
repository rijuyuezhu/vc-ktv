"""A registered guest can browse and download completed songs in this event."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app


def test_guest_history_shows_all_completed_songs_and_downloads(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"history-key",
        auto_prepare=False,
    )
    app.state.events.create("唱歌", "123456")
    with TestClient(app) as a, TestClient(app) as b, TestClient(app) as outsider:
        join_and_register(a, "甲")
        join_and_register(b, "乙")
        store = app.state.store
        with store.lock:
            ids = {
                row["name"]: row["id"]
                for row in store.rows("SELECT id,name FROM guests")
            }
            for i, (name, state, status) in enumerate(
                [
                    ("甲", "done", "played"),
                    ("乙", "manual", "played"),
                    ("甲", "done", "ready"),
                ],
                1,
            ):
                target = store.media / f"video{i}.mp4"
                target.write_bytes(("video" + str(i)).encode())
                store.db.execute(
                    """INSERT INTO songs (bvid,page,title,uploader,cover,duration,
                       requester,requested_at,status,prep_state,media_file,played_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "BV0000000001",
                        i,
                        f"歌{i}",
                        "UP",
                        "",
                        60,
                        ids[name],
                        i,
                        status,
                        state,
                        target.name,
                        i if status == "played" else None,
                    ),
                )
            store.db.commit()
        assert outsider.get("/api/songs/played").status_code == 401
        assert outsider.get("/api/songs/1/detail").status_code == 401
        history = a.get("/api/songs/played")
        assert history.status_code == 200
        songs = history.json()["songs"]
        assert [s["title"] for s in songs] == ["歌2", "歌1"]
        assert all(s["downloadable"] for s in songs)
        assert a.get("/api/songs/2/download").content == b"video2"
        assert b.get("/api/songs/1/download").content == b"video1"
        assert a.get("/api/songs/3/download").content == b"video3"
        detail = b.get("/api/songs/1/detail").json()
        assert detail["mine"] is False and detail["downloadable"] is True
        assert "media_file" not in detail and "source_file" not in detail
        assert a.post("/api/songs/2/cancel").status_code == 403
        assert a.post("/api/songs/1/cancel").status_code == 409
        app.state.events.close()
        assert a.get("/api/songs/played").status_code == 401
        assert a.get("/api/songs/1/download").status_code == 404


def test_played_download_requires_actual_media_and_is_event_scoped(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"history-key",
        auto_prepare=False,
    )
    app.state.events.create("Old", "123456")
    with TestClient(app) as client:
        join_and_register(client, "A")
        store = app.state.store
        with store.lock:
            uid = store.db.execute("select id from guests limit 1").fetchone()[0]
            store.db.execute(
                "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,status,prep_state,media_file,played_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "BV0000000001",
                    1,
                    "消失的片",
                    "UP",
                    "",
                    20,
                    uid,
                    1,
                    "played",
                    "done",
                    "missing.mp4",
                    2,
                ),
            )
            store.db.commit()
        assert client.get("/api/songs/played").json()["songs"][0]["id"] == 1
        assert client.get("/api/songs/1/download").status_code == 404
        app.state.events.close()
        app.state.events.create("New", "654321")
        assert client.get("/api/songs/played").status_code == 401
        assert client.get("/api/songs/1/detail").status_code == 401
