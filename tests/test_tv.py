from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app


def test_tv_token_can_only_be_issued_by_admin(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"secret-test-key",
        auto_prepare=False,
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "test song",
            "uploader": "UP",
            "cover": "",
            "duration": 4,
        }

    app.state.lookup = lookup
    app.state.probe = lambda source: 4
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "访客")
        assert guest.get("/api/admin/tv-link").status_code == 401
        assert guest.get("/api/tv/state", params={"token": "bad"}).status_code == 401
        assert guest.post("/api/tv/next", params={"token": "bad"}).status_code == 401
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        link = admin.get("/api/admin/tv-link").json()["url"]
        token = link.partition("?token=")[2]
        assert token and "tv.html" in link
        assert guest.get("/api/tv/state", params={"token": token}).status_code == 200
        song = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        media = admin.post(
            f"/api/admin/songs/{song}/media",
            files={"file": ("valid.mp4", b"original-video", "video/mp4")},
        )
        assert media.status_code == 200, media.text
        active = guest.post("/api/tv/next", params={"token": token}).json()["active"]
        assert active["id"] == song
        assert guest.get(active["media_url"]).content == b"original-video"
        assert (
            guest.post("/api/tv/next", params={"token": token}).json()["active"]["id"]
            == song
        )
        assert (
            guest.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": song}
            ).status_code
            == 200
        )
        assert (
            guest.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": song}
            ).status_code
            == 409
        )
        assert (
            guest.post("/api/tv/next", params={"token": token}).json()["active"] is None
        )
        assert (
            guest.get("/api/tv/state", params={"token": token}).json()["guests"][0][
                "plays"
            ]
            == 1
        )


def test_tv_preload_is_read_only_and_tracks_queue_changes(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"secret-test-key",
        auto_prepare=False,
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "test song",
            "uploader": "UP",
            "cover": "",
            "duration": 180,
        }

    app.state.lookup = lookup
    app.state.probe = lambda source: 180
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "访客")
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        token = admin.get("/api/admin/tv-link").json()["url"].partition("?token=")[2]
        url = "/api/tv/preload"
        assert guest.get(url).status_code == 422
        assert guest.get(url, params={"token": "wrong"}).status_code == 401
        assert guest.get(url, params={"token": token}).json()["candidate"] is None

        songs = []
        for i in range(3):
            song = guest.post("/api/songs", json={"bvid": f"BV000000000{i}"}).json()[
                "id"
            ]
            assert (
                admin.post(
                    f"/api/admin/songs/{song}/media",
                    files={"file": ("test.mp4", f"media-{i}".encode(), "video/mp4")},
                ).status_code
                == 200
            )
            songs.append(song)

        assert (
            guest.post("/api/tv/next", params={"token": token}).json()["active"]["id"]
            == songs[0]
        )
        before = app.state.store.rows(
            "SELECT id,status,priority_next FROM songs ORDER BY id"
        )
        stats = app.state.store.rows("SELECT plays,fair_offset FROM guests")
        for _ in range(3):
            candidate = guest.get(url, params={"token": token}).json()["candidate"]
            assert candidate["id"] == songs[1]
            assert guest.get(candidate["media_url"]).content == b"media-1"
        assert (
            app.state.store.rows(
                "SELECT id,status,priority_next FROM songs ORDER BY id"
            )
            == before
        )
        assert app.state.store.rows("SELECT plays,fair_offset FROM guests") == stats

        assert admin.post(f"/api/admin/songs/{songs[2]}/pin-next").status_code == 200
        pinned = guest.get(url, params={"token": token}).json()["candidate"]
        assert pinned["id"] == songs[2]
        assert (
            admin.post(
                f"/api/admin/songs/{songs[2]}/media",
                files={"file": ("test.mp4", b"replaced-video", "video/mp4")},
            ).status_code
            == 200
        )
        updated = guest.get(url, params={"token": token}).json()["candidate"]
        assert updated["id"] == songs[2]
        assert updated["media_file"] != pinned["media_file"]
        assert guest.get(updated["media_url"]).content == b"replaced-video"
        assert (
            guest.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": songs[0]}
            ).status_code
            == 200
        )
        assert (
            guest.post("/api/tv/next", params={"token": token}).json()["active"]["id"]
            == songs[2]
        )

        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "manual"}).status_code
            == 409
        )  # Not while a song is playing.
        assert (
            guest.get(url, params={"token": token}).json()["candidate"]["id"]
            == songs[1]
        )
        assert (
            guest.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": songs[2]}
            ).status_code
            == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "manual"}).status_code
            == 200
        )
        assert guest.get(url, params={"token": token}).json()["candidate"] is None
