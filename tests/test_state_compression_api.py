"""Large guest/admin/TV state endpoints preserve JSON and media semantics."""

from fastapi.testclient import TestClient

from tests.helpers import primary_tv_token
from vc_ktv.application.factory import create_app


def test_large_polling_snapshots_are_gzipped_without_touching_video(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        auto_prepare=False,
        secret=b"isolated-state-compression-test",
    )
    app.state.events.create("Test", "123456")
    store = app.state.events.active()
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('user','Guest')")
        store.db.executemany(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,"
            "requested_at,status,prep_state,media_file) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"BV{i:010}",
                    1,
                    f"Large song {i}",
                    "UP",
                    "",
                    90,
                    "user",
                    i,
                    "playing" if i == 0 else "ready",
                    "manual",
                    "source.mp4",
                )
                for i in range(80)
            ],
        )
        store.db.commit()
    media = b"sample-mp4-content" * 4096
    (store.media / "source.mp4").write_bytes(media)
    with TestClient(app) as client:
        login = client.post("/api/admin/login", json={"password": "test-admin"})
        assert login.status_code == 200, login.text
        token = primary_tv_token(client)
        for url in ("/api/state", "/api/admin/state", f"/api/tv/state?token={token}"):
            zipped = client.get(url, headers={"Accept-Encoding": "gzip"})
            plain = client.get(url, headers={"Accept-Encoding": "identity"})
            assert zipped.status_code == 200, zipped.text
            assert plain.status_code == 200, plain.text
            # These are separate requests; a second boundary can change the
            # server clock without changing any application state.
            compressed_state = zipped.json()
            plain_state = plain.json()
            assert isinstance(compressed_state.pop("server_time", None), int)
            assert isinstance(plain_state.pop("server_time", None), int)
            assert compressed_state == plain_state
            assert zipped.headers["content-encoding"] == "gzip"
            assert zipped.headers["vary"] == "Accept-Encoding"
            assert "content-encoding" not in plain.headers
            assert int(zipped.headers["content-length"]) < len(plain.content) // 4
        media_token = app.state.auth.sign(
            "media", f"{app.state.events.active_id}:1", 3600
        )
        response = client.get(
            "/media/1",
            params={"token": media_token},
            headers={"Accept-Encoding": "gzip", "Range": "bytes=0-1023"},
        )
        assert response.status_code == 206
        assert response.content == media[:1024]
        assert "content-encoding" not in response.headers
        assert response.headers["content-range"].startswith("bytes 0-1023/")
