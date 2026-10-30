"""Download current event media for casting through an ordinary phone gallery."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app


def test_event_members_can_download_ready_media_but_not_unfinished_or_old_songs(
    tmp_path,
):
    app = create_app(
        root=tmp_path / "ktv",
        password="test-admin",
        secret=b"test-download-secret",
        auto_prepare=False,
    )
    app.state.events.create("第一场", "123456")
    with (
        TestClient(app) as admin,
        TestClient(app) as guest,
        TestClient(app) as outsider,
    ):
        join_and_register(guest, "点歌者")
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )

        store = app.state.store
        with store.lock:
            user = store.db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
            song_id = store.db.execute(
                """INSERT INTO songs(
                    bvid,page,title,uploader,cover,duration,requester,requested_at,
                    status,prep_state,media_file
                ) VALUES ('BV0000000001',1,'测试成片','测试','',120,?,1,
                          'ready','aligning','1-auto.mp4')""",
                (user,),
            ).lastrowid
            store.db.commit()
        (store.media / "1-auto.mp4").write_bytes(b"test-video-content")
        url = f"/api/songs/{song_id}/download"

        assert outsider.get(url).status_code == 401
        assert guest.get(url).status_code == 409
        assert admin.get(url).status_code == 409
        assert guest.get("/api/state").json()["waiting"][0]["downloadable"] is False

        with store.lock:
            store.db.execute(
                "UPDATE songs SET prep_state='done' WHERE id=?", (song_id,)
            )
            store.db.commit()
        assert guest.get("/api/state").json()["queue"][0]["downloadable"] is True
        result = guest.get(url)
        assert result.status_code == 200
        assert result.content == b"test-video-content"
        assert result.headers["content-type"] == "video/mp4"
        from urllib.parse import unquote

        assert unquote(result.headers["content-disposition"]).endswith(
            f"vc-ktv-{song_id}-测试成片.mp4"
        )
        assert result.headers["content-disposition"].startswith("attachment;")
        assert result.headers["cache-control"] == "private, no-store"
        assert admin.get(url).content == result.content
        assert guest.get(url, headers={"Range": "bytes=0-3"}).content == b"test"

        with store.lock:
            store.db.execute("UPDATE songs SET status='playing' WHERE id=?", (song_id,))
            store.db.commit()
        assert guest.get("/api/state").json()["current"]["downloadable"] is True
        assert guest.get(url).status_code == 200

        with store.lock:
            store.db.execute("UPDATE songs SET status='played' WHERE id=?", (song_id,))
            store.db.commit()
        assert guest.get(url).status_code == 200  # completed songs stay downloadable
        assert guest.get("/api/songs/9999/download").status_code == 404

        with store.lock:
            store.db.execute(
                "UPDATE songs SET status='ready',prep_state='original' WHERE id=?",
                (song_id,),
            )
            store.db.commit()
        assert guest.get(url).status_code == 200

        (store.media / "1-auto.mp4").unlink()
        assert guest.get(url).status_code == 404

        with store.lock:
            store.db.execute("UPDATE songs SET status='played' WHERE id=?", (song_id,))
            store.db.commit()
        assert admin.post("/api/admin/events/close").status_code == 200
        assert guest.get(url).status_code == 404
        assert outsider.get(url).status_code == 404


def test_mobile_download_actions_follow_existing_queue_states():
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "static"
    script = (static / "app.js").read_text()
    assert "if(!song.downloadable)return;" in script
    assert "addDownload(outer,song);" in script
    assert "addDownload(current,state.current);" in script
    assert '"/api/songs/"+song.id+"/download"' in script
    assert "preparationStatus(song)" in script
    assert "乐播投屏" in (static / "index.html").read_text()
