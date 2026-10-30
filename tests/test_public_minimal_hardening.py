"""Constrained public exposure: ordering capacity, pause, and trusted proxy identity."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import MAX_ACTIVE_SONGS, create_app

BVID = "BV0000000001"


def make_app(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"public-minimal-hardening",
        auto_prepare=False,
    )
    app.state.events.create("Party", "123456")
    calls = []

    async def lookup(bvid, page):
        calls.append((bvid, page))
        return {
            "bvid": bvid,
            "page": page,
            "title": "A song",
            "uploader": "Singer",
            "cover": "",
            "duration": 80,
        }

    app.state.lookup = lookup
    return app, calls


def settings(client, **changes):
    return client.put(
        "/api/admin/settings",
        json={"queue_policy": "fair", "allow_unsubtitled": False, **changes},
    )


def order(client):
    return client.post("/api/songs", json={"bvid": BVID})


def fill_songs(store, count, *, status="waiting"):
    with store.lock:
        store.db.execute(
            "INSERT OR IGNORE INTO guests(id,name) VALUES('fixture','Fixture')"
        )
        store.db.executemany(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,
            requester,requested_at,status) VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (BVID, 1, "fixture", "up", "", 60, "fixture", n, status)
                for n in range(count)
            ],
        )
        store.db.commit()


def test_pause_prevents_new_orders_before_lookup_but_keeps_existing_actions(tmp_path):
    app, calls = make_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "Guest")
        assert order(guest).status_code == 200
        song_id = guest.get("/api/state").json()["waiting"][0]["id"]
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert admin.get("/api/admin/settings").json()["orders_paused"] is False
        assert settings(guest, orders_paused=True).status_code == 401
        assert settings(admin, orders_paused=True).json()["orders_paused"] is True
        assert settings(admin, orders_paused="perhaps").status_code == 422
        before = len(calls)
        denied = order(guest)
        assert denied.status_code == 409 and len(calls) == before
        assert guest.get("/api/state").status_code == 200
        assert guest.post(f"/api/songs/{song_id}/cancel").status_code == 200
        assert guest.post("/api/users/logout").status_code == 200
        assert guest.post("/api/join", json={"code": "123456"}).status_code == 200
        assert (
            guest.post(
                "/api/users/login",
                json={"username": "Guest", "password": "test-login-password"},
            ).status_code
            == 200
        )
        assert settings(admin, orders_paused=False).json()["orders_paused"] is False
        assert order(guest).status_code == 200
        # Older admin clients can update unrelated settings without clearing pause.
        settings(admin, orders_paused=True)
        assert settings(admin, video_quality=480).json()["orders_paused"] is True
        assert admin.post("/api/admin/events/close").status_code == 200
        assert admin.post("/api/admin/events", json={"name": "Next"}).status_code == 200
        assert admin.get("/api/admin/settings").json()["orders_paused"] is False


def test_capacity_counts_only_unfinished_songs_and_checks_before_lookup(tmp_path):
    app, calls = make_app(tmp_path)
    with TestClient(app) as guest:
        join_and_register(guest, "Guest")
        fill_songs(app.state.store, MAX_ACTIVE_SONGS - 1)
        assert order(guest).status_code == 200
        assert len(calls) == 1
        assert order(guest).status_code == 409
        assert len(calls) == 1
        with app.state.store.lock:
            app.state.store.db.execute(
                "UPDATE songs SET status='cancelled' WHERE id IN "
                "(SELECT id FROM songs WHERE requester='fixture' LIMIT 2)"
            )
            app.state.store.db.commit()
        assert order(guest).status_code == 200
        assert len(calls) == 2


def test_parallel_orders_and_pause_during_lookup_rechecked(tmp_path):
    app, calls = make_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "Guest")
        admin.post("/api/admin/login", json={"password": "test-admin"})

        # Simulate pause after the preliminary check and before DB insertion.
        async def pause_lookup(bvid, page):
            with app.state.store.lock:
                app.state.store.save_settings("fair", False, orders_paused=True)
            return {
                "bvid": bvid,
                "page": page,
                "title": "test",
                "uploader": "up",
                "cover": "",
                "duration": 50,
            }

        app.state.lookup = pause_lookup
        assert order(guest).status_code == 409
        assert (
            app.state.store.db.execute("SELECT count(*) FROM songs").fetchone()[0] == 0
        )
        settings(admin, orders_paused=False)
        app.state.lookup = lambda bvid, page: asyncio.sleep(
            0,
            result={
                "bvid": bvid,
                "page": page,
                "title": "test",
                "uploader": "up",
                "cover": "",
                "duration": 50,
            },
        )
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: order(guest).status_code, range(6)))
        assert results.count(200) == 3
        assert results.count(409) == 3
        assert len(app.state.store.rows("SELECT id FROM songs")) == 3


def test_proxy_forwarded_ip_only_from_explicit_peer_and_isolated_accounts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VC_KTV_TRUSTED_PROXY_IPS", "192.0.2.10")
    app, _ = make_app(tmp_path)
    bad = {"password": "wrong"}
    with (
        TestClient(app, client=("192.0.2.10", 5511)) as trusted,
        TestClient(app, client=("192.0.2.11", 5511)) as untrusted,
    ):
        for _ in range(10):
            assert (
                trusted.post(
                    "/api/admin/login", json=bad, headers={"X-Real-IP": "198.51.100.1"}
                ).status_code
                == 401
            )
        assert (
            trusted.post(
                "/api/admin/login", json=bad, headers={"X-Real-IP": "198.51.100.1"}
            ).status_code
            == 429
        )
        # Same TT proxy, another client's public IP must not inherit the lock.
        assert (
            trusted.post(
                "/api/admin/login",
                json={"password": "test-admin"},
                headers={"X-Real-IP": "198.51.100.2"},
            ).status_code
            == 200
        )
        # Spoofed headers on direct connections cannot change the throttle key.
        for i in range(10):
            assert (
                untrusted.post(
                    "/api/admin/login",
                    json=bad,
                    headers={"X-Real-IP": f"198.51.100.{i + 1}"},
                ).status_code
                == 401
            )
        assert (
            untrusted.post(
                "/api/admin/login", json=bad, headers={"X-Real-IP": "198.51.100.200"}
            ).status_code
            == 429
        )


def test_malicious_song_text_and_archive_path_are_data_not_code(tmp_path):
    app, _ = make_app(tmp_path)
    malicious = "=1+1\"' OR 1=1 -- <img src=x onerror=alert(1)>"

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": malicious,
            "uploader": malicious,
            "cover": "",
            "duration": 12,
        }

    app.state.lookup = lookup
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "Guest")
        assert order(guest).status_code == 200
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert guest.get("/api/state").json()["waiting"][0]["title"] == malicious
        assert (
            app.state.store.db.execute("SELECT count(*) FROM songs").fetchone()[0] == 1
        )
        assert admin.get(
            "/api/admin/events/../../etc/passwd/songs.csv"
        ).status_code in (404, 400)
        active = app.state.events.active_id
        exported = admin.get(f"/api/admin/events/{active}/songs.csv").text
        assert "'=1+1" in exported  # Spreadsheet formula stays inert.
        assert guest.post("/api/admin/events/close").status_code == 401
        assert guest.get("/media/1?token=../../etc/passwd").status_code == 403
