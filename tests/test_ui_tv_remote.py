from fastapi.testclient import TestClient

from tests.helpers import join_and_register, primary_tv_token
from tests.test_event_admins import create_account, event_login
from vc_ktv.application.factory import create_app


def room(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"tv-remote-mode-tests",
        auto_prepare=False,
    )
    app.state.events.create("KTV", "123456")
    return app


def login(admin):
    response = admin.post("/api/admin/login", json={"password": "pw"})
    assert response.status_code == 200, response.text


def ready_song(app):
    store = app.state.store
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('a','A')")
        (store.media / "song.mp4").write_bytes(b"test media")
        song_id = store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,"
            "requester,requested_at,status,prep_state,media_file,source_file) "
            "VALUES('BV0000000001',1,'Song','UP','',25,'a',1,'ready','done',"
            "'song.mp4','song.mp4')"
        ).lastrowid
        store.db.commit()
    return song_id


def test_only_selected_playback_device_can_claim_and_finish(tmp_path):
    app = room(tmp_path)
    song = ready_song(app)
    with TestClient(app) as admin, TestClient(app) as tv:
        login(admin)
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "browser"}).status_code
            == 200
        )
        token = primary_tv_token(admin)
        assert tv.post("/api/tv/next", params={"token": token}).status_code == 409
        assert admin.post("/api/admin/next").json()["active"]["id"] == song
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 409
        )
        assert (
            admin.post("/api/admin/finish", json={"song_id": song}).status_code == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        assert admin.post("/api/admin/next").status_code == 409
        assert (
            admin.post("/api/admin/finish", json={"song_id": song}).status_code == 409
        )

        second = ready_song_again(app)
        result = tv.post("/api/tv/next", params={"token": token})
        assert result.status_code == 200, result.text
        assert result.json()["active"]["id"] == second
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "manual"}).status_code
            == 409
        )
        assert (
            tv.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": second}
            ).status_code
            == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "manual"}).status_code
            == 200
        )
        assert tv.post("/api/tv/next", params={"token": token}).status_code == 409
        assert admin.post("/api/admin/next").status_code == 200


def ready_song_again(app):
    store = app.state.store
    with store.lock:
        song_id = store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,"
            "requester,requested_at,status,prep_state,media_file,source_file) "
            "VALUES('BV0000000002',1,'Second','UP','',25,'a',2,'ready','done',"
            "'song.mp4','song.mp4')"
        ).lastrowid
        store.db.commit()
    return song_id


def report(tv, token, **overrides):
    data = {
        "ack_seq": 0,
        "active_id": None,
        "paused": True,
        "position": 0,
        "duration": None,
        "volume": 100,
        "offset": 0,
        "opacity": 15,
        "smooth": False,
        "started": True,
        **overrides,
    }
    result = tv.post("/api/tv/report", params={"token": token}, json=data)
    assert result.status_code == 200, result.text


def test_remote_authorization_song_identity_ack_and_primary_handoff(tmp_path):
    app = room(tmp_path)
    song = ready_song(app)
    with TestClient(app) as admin, TestClient(app) as tv, TestClient(app) as guest:
        login(admin)
        join_and_register(guest, "Singer")
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        token = primary_tv_token(admin)
        assert (
            guest.post(
                "/api/admin/tv/remote", json={"action": "volume", "value": 25}
            ).status_code
            == 401
        )
        assert (
            guest.post(
                "/api/tv/report", params={"token": "wrong"}, json={"ack_seq": 0}
            ).status_code
            == 401
        )
        assert (
            tv.post("/api/tv/next", params={"token": token}).json()["active"]["id"]
            == song
        )
        report(tv, token, active_id=song, paused=False)
        assert admin.get("/api/admin/tv/remote").json()["online"]
        assert (
            admin.post(
                "/api/admin/tv/remote", json={"action": "skip", "song_id": song + 1}
            ).status_code
            == 409
        )
        assert admin.post(
            "/api/admin/tv/remote", json={"action": "volume", "value": 101}
        ).status_code in (400, 422)
        result = admin.post(
            "/api/admin/tv/remote", json={"action": "pause", "song_id": song}
        )
        assert result.status_code == 200, result.text
        seq = result.json()["seq"]
        command = tv.get("/api/tv/state", params={"token": token}).json()[
            "remote_command"
        ]
        assert command == {
            "seq": seq,
            "action": "pause",
            "song_id": song,
            "value": None,
        }
        assert (
            admin.post(
                "/api/admin/tv/remote", json={"action": "skip", "song_id": song}
            ).status_code
            == 409
        )
        report(tv, token, ack_seq=seq, active_id=song, paused=True)
        state = admin.get("/api/admin/tv/remote").json()
        assert not state["pending"]
        assert state["report"]["paused"] is True
        assert (
            tv.get("/api/tv/state", params={"token": token}).json()["remote_command"]
            is None
        )
        next_command = admin.post(
            "/api/admin/tv/remote", json={"action": "replay_next", "song_id": song}
        )
        assert next_command.status_code == 200, next_command.text
        assert next_command.json()["seq"] == seq + 1
        replacement = primary_tv_token(admin, replace=True)
        assert tv.get("/api/tv/state", params={"token": token}).status_code == 401
        assert (
            tv.post(
                "/api/tv/report", params={"token": token}, json={"ack_seq": seq + 1}
            ).status_code
            == 401
        )
        assert admin.get("/api/admin/tv/remote").json()["report"] == {}
        report(tv, replacement)
        assert admin.get("/api/admin/tv/remote").json()["online"]


def test_event_admin_cannot_remote_control_global_primary(tmp_path):
    app = room(tmp_path)
    with (
        TestClient(app) as global_admin,
        TestClient(app) as event_admin,
        TestClient(app) as tv,
    ):
        login(global_admin)
        create_account(global_admin)
        assert event_login(event_admin).status_code == 200
        assert (
            global_admin.put(
                "/api/admin/playback-mode", json={"mode": "tv"}
            ).status_code
            == 200
        )
        token = primary_tv_token(global_admin)
        report(tv, token)
        assert event_admin.get("/api/admin/tv/remote").json()["can_manage"] is False
        assert (
            event_admin.post(
                "/api/admin/tv/remote", json={"action": "volume", "value": 42}
            ).status_code
            == 403
        )
        assert (
            global_admin.post(
                "/api/admin/tv/remote", json={"action": "volume", "value": 42}
            ).status_code
            == 200
        )


def test_default_tv_mode_and_explicit_browser_choice_persist(tmp_path):
    app = room(tmp_path)
    with TestClient(app) as admin:
        login(admin)
        assert admin.get("/api/admin/state").json()["playback_mode"] == "tv"
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "browser"}).status_code
            == 200
        )
        assert admin.get("/api/admin/state").json()["playback_mode"] == "browser"
        assert admin.post("/api/admin/events/close").status_code == 200
        created = admin.post("/api/admin/events", json={"name": "Next Room"})
        assert created.status_code == 200, created.text
        assert admin.get("/api/admin/state").json()["playback_mode"] == "tv"
