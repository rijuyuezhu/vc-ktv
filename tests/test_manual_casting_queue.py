"""Shared queue transitions for self-cancel, replay, and hand-operated casting."""

from pathlib import Path

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app


def new_app(tmp_path):
    app = create_app(
        root=tmp_path / "ktv",
        password="test-admin",
        secret=b"manual-casting-test-secret",
        auto_prepare=False,
    )
    app.state.events.create("本场测试", "123456")
    return app


def add_song(store, requester, *, status="ready", prep="done", media=None, time=1):
    with store.lock:
        store.db.execute(
            "INSERT OR IGNORE INTO guests(id,name) VALUES (?,?)",
            (requester, requester),
        )
        song_id = store.db.execute(
            """INSERT INTO songs(
                bvid,page,title,uploader,cover,duration,requester,requested_at,
                status,prep_state,media_file,source_file
            ) VALUES ('BV0000000001',1,'重唱测试','测试','',180,?,?,?,?,
                      ?,?)""",
            (requester, time, status, prep, media, media),
        ).lastrowid
        store.db.commit()
    if media:
        (store.media / media).write_bytes(b"valid-pretend-mp4")
    return song_id


def test_user_can_only_cancel_own_unstarted_songs(tmp_path):
    app = new_app(tmp_path)
    with TestClient(app) as a, TestClient(app) as b, TestClient(app) as outsider:
        join_and_register(a, "Alice")
        join_and_register(b, "Bob")
        store = app.state.store
        alice = store.db.execute(
            "SELECT id FROM users WHERE username='Alice'"
        ).fetchone()[0]
        bob = store.db.execute("SELECT id FROM users WHERE username='Bob'").fetchone()[
            0
        ]
        ready = add_song(store, alice, time=1, media="ready.mp4")
        aligning = add_song(store, alice, time=2, prep="aligning", media="pending.mp4")
        downloading = add_song(
            store, alice, time=3, status="waiting", prep="downloading"
        )
        other = add_song(store, bob, time=4, media="other.mp4")

        assert outsider.post(f"/api/songs/{ready}/cancel").status_code == 401
        assert b.post(f"/api/songs/{ready}/cancel").status_code == 403
        assert a.post("/api/songs/999/cancel").status_code == 404
        for song in (ready, aligning, downloading):
            assert a.post(f"/api/songs/{song}/cancel").status_code == 200
            assert a.post(f"/api/songs/{song}/cancel").status_code == 409
        state = a.get("/api/state").json()
        assert {x["id"] for x in state["queue"]} == {other}
        assert state["waiting"] == []

        # Completion of an in-flight preparation cannot requeue a cancelled item.
        with store.lock:
            store.db.execute(
                "UPDATE songs SET prep_state='done' WHERE id=?", (aligning,)
            )
            store.db.commit()
        assert all(x["id"] != aligning for x in a.get("/api/state").json()["queue"])
        assert b.post(f"/api/songs/{other}/cancel").status_code == 200


def test_replay_prioritized_next_in_both_policies_and_keeps_history(tmp_path):
    app = new_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "A")
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        store = app.state.store
        a = store.db.execute("SELECT id FROM users").fetchone()[0]
        first = add_song(store, a, time=1, media="first.mp4")
        other = add_song(store, a, time=2, media="other.mp4")
        before = admin.get("/api/admin/played")
        assert before.json()["songs"] == []
        assert guest.post(f"/api/admin/songs/{first}/replay").status_code == 401
        assert admin.post(f"/api/admin/songs/{first}/replay").status_code == 409
        assert admin.post("/api/admin/songs/991/replay").status_code == 404
        assert admin.post("/api/admin/next").json()["active"]["id"] == first
        assert (
            admin.post("/api/admin/finish", json={"song_id": first}).status_code == 200
        )
        assert admin.get("/api/admin/played").json()["songs"][0]["id"] == first
        new = admin.post(f"/api/admin/songs/{first}/replay").json()["id"]
        assert new != first
        assert admin.get("/api/state").json()["queue"][0]["id"] == new
        assert admin.get("/api/state").json()["queue"][0]["priority_next"] == 1
        assert admin.get(f"/api/songs/{new}/download").content == b"valid-pretend-mp4"
        assert (store.media / "first.mp4").read_bytes() == b"valid-pretend-mp4"

        # Priority is independent of fairness/FCFS settings.
        assert (
            admin.put(
                "/api/admin/settings",
                json={"queue_policy": "fcfs", "allow_unsubtitled": False},
            ).status_code
            == 200
        )
        assert [s["id"] for s in admin.get("/api/state").json()["queue"]] == [
            new,
            other,
        ]
        assert admin.post("/api/admin/next").json()["active"]["id"] == new
        assert admin.get("/api/state").json()["current"]["priority_next"] == 0
        assert admin.post("/api/admin/finish", json={"song_id": new}).status_code == 200
        assert admin.post("/api/admin/next").json()["active"]["id"] == other
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM songs WHERE status='played'"
            ).fetchone()[0]
            == 2
        )
        assert (
            store.db.execute("SELECT plays FROM guests WHERE id=?", (a,)).fetchone()[0]
            == 2
        )
        assert (
            store.db.execute(
                "SELECT replay_of FROM songs WHERE id=?", (new,)
            ).fetchone()[0]
            == first
        )
        assert (
            store.db.execute(
                "SELECT status FROM songs WHERE id=?", (first,)
            ).fetchone()[0]
            == "played"
        )

        assert admin.post(f"/api/admin/songs/{other}/replay").status_code == 409
        assert guest.post(f"/api/songs/{other}/cancel").status_code == 409


def test_priority_replay_during_current_manual_cast_next_and_tv_guard(tmp_path):
    app = new_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "Singer")
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        store = app.state.store
        singer = store.db.execute("SELECT id FROM users").fetchone()[0]
        first = add_song(store, singer, media="song.mp4", time=1)
        second = add_song(store, singer, media="next.mp4", time=2)
        third = add_song(store, singer, media="later.mp4", time=3)
        assert (
            guest.put("/api/admin/playback-mode", json={"mode": "manual"}).status_code
            == 401
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "wrong"}).status_code
            == 422
        )
        assert admin.put(
            "/api/admin/playback-mode", json={"mode": "manual"}
        ).json() == {"mode": "manual"}
        assert guest.get("/api/state").json()["playback_mode"] == "manual"
        token = admin.get("/api/admin/tv-link").json()["url"].partition("?token=")[2]
        assert guest.post("/api/tv/next", params={"token": token}).status_code == 409
        assert admin.post("/api/admin/next").json()["active"]["id"] == first
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "browser"}).status_code
            == 409
        )
        assert (
            guest.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": first}
            ).status_code
            == 409
        )
        assert (
            admin.post("/api/admin/finish", json={"song_id": first}).status_code == 200
        )
        assert admin.get("/api/admin/played").json()["songs"][0]["id"] == first
        assert admin.post("/api/admin/next").json()["active"]["id"] == second
        replay = admin.post(f"/api/admin/songs/{first}/replay").json()["id"]
        assert admin.get("/api/state").json()["queue"][0]["id"] == replay
        assert (
            admin.post(
                "/api/admin/finish", json={"song_id": second, "reason": "skip"}
            ).status_code
            == 200
        )
        assert admin.post("/api/admin/next").json()["active"]["id"] == replay
        assert (
            admin.post(
                "/api/admin/finish", json={"song_id": replay, "reason": "skip"}
            ).status_code
            == 200
        )
        assert admin.post("/api/admin/next").json()["active"]["id"] == third
        assert (
            admin.post("/api/admin/finish", json={"song_id": third}).status_code == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "browser"}).status_code
            == 200
        )
        assert guest.get("/api/state").json()["playback_mode"] == "browser"


def test_playback_mode_persists_in_event_and_new_event_defaults_to_browser(tmp_path):
    root = tmp_path / "ktv"
    app = new_app(tmp_path)
    with TestClient(app) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "manual"}).status_code
            == 200
        )
    reopened = create_app(
        root=root,
        password="test-admin",
        secret=b"manual-casting-test-secret",
        auto_prepare=False,
    )
    with TestClient(reopened) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        assert admin.get("/api/admin/state").json()["playback_mode"] == "manual"
        assert admin.post("/api/admin/events/close").status_code == 200
        new_event = admin.post("/api/admin/events", json={"name": "新场次"})
        assert new_event.status_code == 200
        assert admin.get("/api/admin/state").json()["playback_mode"] == "browser"


def test_manual_controls_ui_present_and_not_a_second_queue():
    static = Path(__file__).resolve().parents[1] / "static"
    html = (static / "index.html").read_text()
    js = (static / "app.js").read_text()
    for element in (
        "playback-mode",
        "manual-controls",
        "manual-download",
        "played-panel",
        "played-list",
        "browser-player",
        "autoplay-label",
    ):
        assert f'id="{element}"' in html
    assert 'api("/api/songs/"+song.id+"/cancel","POST")' in js
    assert 'api("/api/admin/songs/"+song.id+"/replay","POST")' in js
    assert 'api("/api/admin/playback-mode","PUT",{mode})' in js
    assert '"manual-download").href="/api/songs/"+active.id+"/download"' in js
    assert "if(!manualMode)guarded" in js
