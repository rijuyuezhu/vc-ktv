import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app, get_bvid


@pytest.fixture
def room(tmp_path):
    app = create_app(
        root=tmp_path / "data",
        password="test-password",
        secret=b"test-hmac-secret",
        auto_prepare=False,
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": f"曲目 {bvid} P{page}",
            "uploader": "测试 UP",
            "cover": "https://example.com/cover.png",
            "duration": 180 + page,
            "part": f"第{page}P",
            "pages": [{"page": 1, "part": "P1"}, {"page": 2, "part": "P2"}],
            "official_url": "https://www.bilibili.com/video/" + bvid,
            "embed_url": "https://player.bilibili.com/player.html?bvid=" + bvid,
        }

    app.state.lookup = lookup
    app.state.probe = lambda path: 181.0
    return app


def post_song(client, bvid="BV0000000004", nick="小 A", page=1):
    r = client.post("/api/songs", json={"bvid": bvid, "nickname": nick, "page": page})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def login(client):
    assert (
        client.post("/api/admin/login", json={"password": "test-password"}).status_code
        == 200
    )


def upload(client, song_id, body=b"test-media-content"):
    return client.post(
        f"/api/admin/songs/{song_id}/media",
        files={"file": ("demo.mp4", body, "video/mp4")},
    )


def test_security_and_metadata(room):
    with TestClient(room) as c:
        join_and_register(c, "客户")
        assert c.get("/api/health").json()["ok"]
        assert c.get("/").status_code == 200
        assert c.get("/api/preview", params={"bvid": "not valid"}).status_code == 400
        assert (
            get_bvid("https://www.bilibili.com/video/BV0000000004/?p=1")
            == "BV0000000004"
        )
        assert (
            c.get(
                "/api/preview", params={"bvid": "BV0000000004", "page": 3}
            ).status_code
            == 200
        )  # mocked lookup
        assert c.post("/api/admin/next").status_code == 401
        assert c.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
        assert (
            c.post(
                "/api/admin/login",
                json={"password": "test-password"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        login(c)
        assert c.get("/api/admin/state").status_code == 200
        assert c.post("/api/admin/logout").status_code == 200
        assert c.get("/api/admin/state").status_code == 401


def test_queue_is_ready_gated_and_fair(room):
    with TestClient(room) as admin, TestClient(room) as a, TestClient(room) as b:
        join_and_register(a, "A")
        join_and_register(b, "B")
        login(admin)
        a1 = post_song(a, nick="A", page=1)
        a2 = post_song(a, nick="A", page=2)
        b1 = post_song(b, nick="B", page=1)
        # No source media => no claim that playback works.
        assert len(admin.get("/api/state").json()["waiting"]) == 3
        assert admin.post("/api/admin/next").json()["active"] is None
        for song_id in (a1, a2, b1):
            assert upload(admin, song_id).status_code == 200
        state = admin.get("/api/state").json()
        assert [x["id"] for x in state["queue"]] == [a1, b1, a2]
        active = admin.post("/api/admin/next").json()["active"]
        assert active["id"] == a1
        assert admin.post("/api/admin/next").json()["active"]["id"] == a1  # idempotent
        assert admin.post("/api/admin/finish", json={"song_id": a1}).status_code == 200
        assert admin.post("/api/admin/finish", json={"song_id": a1}).status_code == 409
        assert admin.post("/api/admin/next").json()["active"]["id"] == b1
        assert (
            admin.post(
                "/api/admin/finish", json={"song_id": b1, "reason": "skip"}
            ).status_code
            == 200
        )
        assert admin.post("/api/admin/next").json()["active"]["id"] == a2
        stats = admin.get("/api/state").json()["guests"]
        assert next(x for x in stats if x["name"] == "A")["plays"] == 1
        assert next(x for x in stats if x["name"] == "B")["plays"] == 0


def test_signed_media_and_range(room):
    with TestClient(room) as admin, TestClient(room) as guest:
        join_and_register(guest, "访客")
        login(admin)
        song = post_song(guest)
        assert upload(guest, song).status_code == 401
        assert upload(admin, song, b"abcdefghijklm").status_code == 200
        active = admin.post("/api/admin/next").json()["active"]
        assert active["id"] == song
        assert admin.get(f"/media/{song}").status_code == 403
        assert admin.get(active["media_url"]).content == b"abcdefghijklm"
        part = admin.get(active["media_url"], headers={"Range": "bytes=2-5"})
        assert part.status_code == 206
        assert part.content == b"cdef"
        assert admin.post(f"/api/admin/songs/{song}/cancel").status_code == 409


def test_limits_and_permissions(room):
    with TestClient(room) as admin, TestClient(room) as guest:
        join_and_register(guest, "访客")
        login(admin)
        songs = [post_song(guest, page=1) for i in range(3)]
        fourth = guest.post(
            "/api/songs", json={"bvid": "BV0000000004", "nickname": "A", "page": 1}
        )
        assert fourth.status_code == 409
        assert (
            admin.post(
                f"/api/admin/songs/{songs[0]}/media",
                files={"file": ("text.txt", b"a", "text/plain")},
            ).status_code
            == 400
        )
        assert (
            admin.post(
                f"/api/admin/songs/{songs[0]}/lyrics", json={"text": "歌词"}
            ).status_code
            == 200
        )
        assert admin.get(f"/api/admin/songs/{songs[0]}/lyrics").json()["text"] == "歌词"
        assert guest.get(f"/api/admin/songs/{songs[0]}/lyrics").status_code == 401
        assert admin.post(f"/api/admin/songs/{songs[0]}/cancel").status_code == 200
        assert len(admin.get("/api/state").json()["waiting"]) == 2


def test_admin_subpage_and_existing_cookie_auth(room):
    with TestClient(room) as client:
        for path in ("/admin", "/admin/"):
            page = client.get(path)
            assert page.status_code == 200
            assert 'href="/admin/"' in page.text
            assert 'id="admin-view"' in page.text
            assert 'id="guest-view"' in page.text
        assert client.get("/").status_code == 200
        assert client.get("/api/admin/state").status_code == 401
        login(client)
        assert client.get("/admin/").status_code == 200
        assert client.get("/api/admin/state").status_code == 200
