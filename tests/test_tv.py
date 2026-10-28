from fastapi.testclient import TestClient

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
