"""Guest replay is an ordinary turn; source download is scoped to the current event."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app


def test_guest_replay_does_not_steal_admin_priority_and_keeps_owner(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"ui-detail-key",
        auto_prepare=False,
    )
    app.state.events.create("唱歌", "123456")
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as outsider:
        join_and_register(alice, "甲")
        join_and_register(bob, "乙")
        store = app.state.store
        with store.lock:
            owners = {
                r["name"]: r["id"] for r in store.rows("SELECT id,name FROM guests")
            }
            for name in ("rendered.mp4", "original.mp4", "pinned.mp4"):
                (store.media / name).write_bytes(name.encode())
            store.db.execute(
                """INSERT INTO songs (bvid,page,title,uploader,cover,duration,requester,
                   requested_at,status,prep_state,media_file,source_file,played_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "BV1234567890",
                    1,
                    "旧歌",
                    "作者",
                    "",
                    140,
                    owners["甲"],
                    1,
                    "played",
                    "done",
                    "rendered.mp4",
                    "original.mp4",
                    2,
                ),
            )
            store.db.execute(
                """INSERT INTO songs (bvid,page,title,uploader,cover,duration,requester,
                   requested_at,status,prep_state,media_file,source_file,priority_next)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "BV1234567891",
                    1,
                    "管理员置顶",
                    "作者",
                    "",
                    140,
                    owners["甲"],
                    3,
                    "ready",
                    "done",
                    "pinned.mp4",
                    "pinned.mp4",
                    1,
                ),
            )
            store.db.commit()

        assert outsider.get("/api/songs/1/download/source").status_code == 401
        detail = bob.get("/api/songs/1/detail").json()
        assert detail["source_downloadable"] is True
        assert detail["uploader"] == "作者"
        assert "source_file" not in detail
        assert bob.get("/api/songs/1/download/source").content == b"original.mp4"
        assert bob.get("/api/songs/1/download").content == b"rendered.mp4"
        response = bob.post("/api/songs/1/replay")
        assert response.status_code == 200, response.text
        item = response.json()
        assert item["priority_next"] is False
        with store.lock:
            replay = store.db.execute(
                "SELECT requester,status,priority_next,replay_of,media_file,source_file "
                "FROM songs WHERE id=?",
                (item["id"],),
            ).fetchone()
            assert replay["requester"] == owners["乙"]
            assert replay["status"] == "ready"
            assert replay["priority_next"] == 0
            assert replay["replay_of"] == 1
            assert (
                store.db.execute(
                    "SELECT priority_next FROM songs WHERE id=2"
                ).fetchone()[0]
                == 1
            )
            assert (store.media / replay["media_file"]).read_bytes() == b"rendered.mp4"
            assert (store.media / replay["source_file"]).read_bytes() == b"original.mp4"
        assert (
            bob.get("/api/songs/" + str(item["id"]) + "/detail").json()["mine"] is True
        )
        store.save_settings("fair", False, orders_paused=True)
        assert bob.post("/api/songs/1/replay").status_code == 409
        app.state.events.close()
        assert bob.get("/api/songs/1/download/source").status_code == 404
