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


def test_tv_replay_next_is_scoped_idempotent_and_preserves_fairness(tmp_path):
    app = create_app(
        root=tmp_path / "room", password="pw", secret=b"tv-next", auto_prepare=False
    )
    app.state.events.create("TV Room", "123456")
    with TestClient(app) as admin, TestClient(app) as a, TestClient(app) as b:
        join_and_register(a, "A")
        join_and_register(b, "B")
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        token = admin.get("/api/admin/tv-link").json()["url"].partition("?token=")[2]
        store = app.state.store
        for name in ("A", "B"):
            guest_id = store.rows("SELECT id FROM guests WHERE name=?", (name,))[0][
                "id"
            ]
            with store.lock:
                for idx in range(2):
                    path = store.media / f"{name}-{idx}.mp4"
                    path.write_bytes(f"{name}{idx}".encode())
                    store.db.execute(
                        """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,
                        status,prep_state,media_file,source_file)
                        VALUES('BV0000000001',1,?,'up','',90,?,?,'ready','manual',?,?)""",
                        (name, guest_id, idx, path.name, path.name),
                    )
                store.db.commit()
        current = a.post("/api/tv/next", params={"token": token}).json()["active"]
        assert current["nickname"] == "A"
        state = a.get("/api/tv/state", params={"token": token}).json()
        assert state["queue"][0]["nickname"] == "B"
        before = store.rows("SELECT plays,fair_offset FROM guests ORDER BY name")
        route = "/api/tv/replay-next"
        assert (
            a.post(
                route, params={"token": "bad"}, json={"song_id": current["id"]}
            ).status_code
            == 401
        )
        assert (
            a.post(route, params={"token": token}, json={"song_id": 999}).status_code
            == 409
        )
        first = a.post(route, params={"token": token}, json={"song_id": current["id"]})
        assert first.status_code == 200, first.text
        new_id = first.json()["id"]
        assert new_id != current["id"]
        assert (
            a.post(
                route, params={"token": token}, json={"song_id": current["id"]}
            ).json()["id"]
            == new_id
        )
        assert store.rows(
            "SELECT id FROM songs WHERE replay_of=?", (current["id"],)
        ) == [{"id": new_id}]
        assert (
            store.rows("SELECT plays,fair_offset FROM guests ORDER BY name") == before
        )
        # Admin can override the next slot; a repeat tap moves the SAME replay back.
        other_id = store.rows("SELECT id FROM songs WHERE title='B' ORDER BY id")[0][
            "id"
        ]
        assert admin.post(f"/api/admin/songs/{other_id}/pin-next").status_code == 200
        assert (
            a.post(
                route, params={"token": token}, json={"song_id": current["id"]}
            ).json()["id"]
            == new_id
        )
        assert (
            a.get("/api/tv/state", params={"token": token}).json()["queue"][0]["id"]
            == new_id
        )
        assert (
            a.post(
                "/api/tv/finish",
                params={"token": token},
                json={"song_id": current["id"]},
            ).status_code
            == 200
        )
        assert (
            a.post(
                route, params={"token": token}, json={"song_id": current["id"]}
            ).status_code
            == 409
        )
        assert (
            a.post("/api/tv/next", params={"token": token}).json()["active"]["id"]
            == new_id
        )
        assert (
            a.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": new_id}
            ).status_code
            == 200
        )
        assert store.rows("SELECT plays,fair_offset FROM guests ORDER BY name") == [
            {"plays": 1, "fair_offset": 0},
            {"plays": 0, "fair_offset": 0},
        ]
        assert store.rows(
            "SELECT replay_of,fair_exempt FROM songs WHERE id=?", (new_id,)
        ) == [{"replay_of": current["id"], "fair_exempt": 1}]
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "manual"}).status_code
            == 200
        )
        other = admin.post("/api/admin/next").json()["active"]
        assert other is not None
        assert (
            a.post(
                route, params={"token": token}, json={"song_id": other["id"]}
            ).status_code
            == 409
        )


def test_tv_live_next_up_tracks_three_people_pin_and_cancel(tmp_path):
    from tests.test_fair_capped_two import party, ready

    app, guests, admin, stack = party(tmp_path, ["A", "B", "C"])
    with stack:
        store = app.state.store
        for name in guests:
            ready(store, name, 2)
        token = admin.get("/api/admin/tv-link").json()["url"].partition("?token=")[2]
        first = admin.post("/api/admin/next").json()["active"]
        assert first["nickname"] == "A"
        before = store.rows("SELECT plays,fair_offset FROM guests ORDER BY name")
        get_queue = lambda: (
            guests["A"].get("/api/tv/state", params={"token": token}).json()["queue"]
        )
        assert get_queue()[0]["nickname"] == "B"
        target = next(x for x in get_queue() if x["nickname"] == "C")
        assert (
            admin.post(f"/api/admin/songs/{target['id']}/pin-next").status_code == 200
        )
        assert get_queue()[0]["id"] == target["id"]
        assert guests["C"].post(f"/api/songs/{target['id']}/cancel").status_code == 200
        assert get_queue()[0]["nickname"] == "B"
        assert (
            store.rows("SELECT plays,fair_offset FROM guests ORDER BY name") == before
        )
        assert (
            admin.post(
                "/api/admin/finish", json={"song_id": first["id"], "reason": "skip"}
            ).status_code
            == 200
        )
        assert admin.post("/api/admin/next").json()["active"]["nickname"] == "B"
