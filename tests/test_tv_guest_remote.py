"""Requester-only TV remote authorization and admin priority regressions."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register, primary_tv_token
from tests.test_event_admins import create_account, event_login
from vc_ktv.application.factory import create_app
from vc_ktv.processing.backing_audio import KARAOKE_SHA256, source_identity


def room(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"guest-remote-regression-secret",
        auto_prepare=False,
    )
    app.state.events.create("KTV", "123456")
    return app


def setup_song(app, user_id, *, source_name="song.mp4"):
    store = app.state.store
    with store.lock:
        (store.media / source_name).write_bytes(b"sample-source" * 200)
        song_id = store.db.execute(
            """INSERT INTO songs(
                bvid,page,title,uploader,cover,duration,requester,requested_at,
                status,prep_state,media_file,source_file)
                VALUES('BV0000000001',1,'Song','UP','',25,?,1,'ready',
                       'done',?,?)""",
            (user_id, source_name, source_name),
        ).lastrowid
        store.db.commit()
    return song_id


def guest_id(app, name):
    row = app.state.store.db.execute(
        "SELECT id FROM guests WHERE name=?", (name,)
    ).fetchone()
    return row["id"]


def prepare_backing(app, song_id):
    store = app.state.store
    with store.lock:
        row = dict(
            store.db.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
        )
        source = store.media / row["source_file"]
        key = source_identity(source, KARAOKE_SHA256)
        directory = store.root / "backing"
        directory.mkdir(exist_ok=True)
        (directory / f"{key}.m4a").write_bytes(b"AAC sample" * 500)
        store.db.execute(
            """INSERT INTO backing_tracks(cache_key,source_file,model_sha256,status)
               VALUES(?,?,?,'ready')""",
            (key, source.name, KARAOKE_SHA256),
        )
        store.db.commit()


def report(
    tv,
    token,
    *,
    ack_seq=0,
    active_id=None,
    paused=False,
    audio_mode="original",
    overlay=False,
    offset=0,
    duration=None,
):
    response = tv.post(
        "/api/tv/report",
        params={"token": token},
        json={
            "ack_seq": ack_seq,
            "active_id": active_id,
            "paused": paused,
            "audio_mode": audio_mode,
            "position": 15,
            "duration": duration,
            "overlay": overlay,
            "offset": offset,
            "started": True,
        },
    )
    assert response.status_code == 200, response.text


def command(client, action, song_id, value=None):
    return client.post(
        "/api/guest/tv-remote",
        json={"action": action, "song_id": song_id, "value": value},
    )


def setup_clients(app, owner, singer, other, tv):
    assert owner.post("/api/admin/login", json={"password": "pw"}).status_code == 200
    join_and_register(singer, "Singer A")
    join_and_register(other, "Singer B")
    song = setup_song(app, guest_id(app, "Singer A"))
    assert owner.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code == 200
    token = primary_tv_token(owner)
    response = tv.post("/api/tv/next", params={"token": token})
    assert response.status_code == 200 and response.json()["active"]["id"] == song
    report(tv, token, active_id=song)
    return song, token


def test_only_current_requester_can_see_and_send_transport(tmp_path):
    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv,
        TestClient(app) as outsider,
    ):
        song, token = setup_clients(app, admin, singer, other, tv)
        assert outsider.get("/api/guest/tv-remote").status_code == 401
        assert command(outsider, "pause", song).status_code == 401
        assert other.get("/api/guest/tv-remote").json() == {"available": False}
        assert command(other, "pause", song).status_code == 403
        assert command(singer, "volume", song, 25).status_code == 403
        assert command(singer, "replay_next", song).status_code == 403
        assert command(singer, "pause", song + 1).status_code == 409
        state = singer.get("/api/guest/tv-remote").json()
        assert state == {
            "available": True,
            "song_id": song,
            "online": True,
            "pending": False,
            "position": 15,
            "duration": None,
            "overlay": False,
            "offset": None,
            "paused": False,
            "audio_mode": "original",
            "backing_ready": False,
            "error": "",
        }
        for action in ("pause", "resume", "restart", "skip"):
            response = command(singer, action, song)
            assert response.status_code == 200, response.text
            seq = response.json()["seq"]
            dispatched = tv.get("/api/tv/state", params={"token": token}).json()[
                "remote_command"
            ]
            assert dispatched == {
                "seq": seq,
                "action": action,
                "song_id": song,
                "value": None,
            }
            assert singer.get("/api/guest/tv-remote").json()["pending"]
            assert command(singer, "pause", song).status_code == 409
            report(tv, token, ack_seq=seq, active_id=song)
            assert not singer.get("/api/guest/tv-remote").json()["pending"]


def test_guest_audio_mode_requires_ready_asset_and_report_tracks_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VC_KTV_BACKING_TRACKS", "1")
    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv,
    ):
        song, token = setup_clients(app, admin, singer, other, tv)
        assert command(singer, "audio_mode", song, "backing").status_code == 409
        assert command(singer, "audio_mode", song, "invalid").status_code in (400, 422)
        prepare_backing(app, song)
        assert singer.get("/api/guest/tv-remote").json()["backing_ready"]
        switched = command(singer, "audio_mode", song, "backing")
        assert switched.status_code == 200, switched.text
        assert (
            tv.get("/api/tv/state", params={"token": token}).json()["remote_command"][
                "value"
            ]
            == "backing"
        )
        report(
            tv,
            token,
            ack_seq=switched.json()["seq"],
            active_id=song,
            audio_mode="backing",
        )
        state = singer.get("/api/guest/tv-remote").json()
        assert state["audio_mode"] == "backing"
        assert state["backing_ready"]
        original = command(singer, "audio_mode", song, "original")
        assert original.status_code == 200
        report(tv, token, ack_seq=original.json()["seq"], active_id=song)


def test_admin_preempts_guest_but_not_other_admin_inflight(tmp_path):
    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv,
    ):
        song, token = setup_clients(app, admin, singer, other, tv)
        guest = command(singer, "restart", song)
        assert guest.status_code == 200
        first_seq = guest.json()["seq"]
        assert command(singer, "skip", song).status_code == 409
        takeover = admin.post(
            "/api/admin/tv/remote", json={"action": "pause", "song_id": song}
        )
        assert takeover.status_code == 200, takeover.text
        assert takeover.json()["seq"] == first_seq + 1
        assert (
            tv.get("/api/tv/state", params={"token": token}).json()["remote_command"][
                "action"
            ]
            == "pause"
        )
        assert command(singer, "skip", song).status_code == 409
        assert (
            admin.post(
                "/api/admin/tv/remote", json={"action": "skip", "song_id": song}
            ).status_code
            == 409
        )
        # A delayed ACK for the overridden guest command must NOT clear the admin command.
        report(tv, token, ack_seq=first_seq, active_id=song)
        assert (
            tv.get("/api/tv/state", params={"token": token}).json()["remote_command"][
                "seq"
            ]
            == first_seq + 1
        )
        report(tv, token, ack_seq=first_seq + 1, active_id=song)
        assert command(singer, "skip", song).status_code == 200


def test_global_admin_preempts_event_admin_and_permissions_remain(tmp_path):
    app = room(tmp_path)
    with (
        TestClient(app) as global_admin,
        TestClient(app) as event_admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv,
    ):
        song, _ = setup_clients(app, global_admin, singer, other, tv)
        create_account(global_admin)
        assert event_login(event_admin).status_code == 200
        # Event admin cannot control a global-admin-owned primary.
        assert (
            event_admin.post(
                "/api/admin/tv/remote", json={"action": "pause", "song_id": song}
            ).status_code
            == 403
        )
        # Event-owned primary is lower priority than global administrator commands.
        assert global_admin.post("/api/admin/tv/revoke").status_code == 200
        token = primary_tv_token(event_admin)
        assert (
            tv.post("/api/tv/next", params={"token": token}).json()["active"]["id"]
            == song
        )
        report(tv, token, active_id=song)
        event_command = event_admin.post(
            "/api/admin/tv/remote", json={"action": "pause", "song_id": song}
        )
        assert event_command.status_code == 200
        assert (
            global_admin.post(
                "/api/admin/tv/remote", json={"action": "resume", "song_id": song}
            ).status_code
            == 200
        )
        assert (
            event_admin.post(
                "/api/admin/tv/remote", json={"action": "skip", "song_id": song}
            ).status_code
            == 409
        )


def test_song_handoff_offline_primary_and_tv_mode_reject_stale_guest(tmp_path):
    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv,
    ):
        song, token = setup_clients(app, admin, singer, other, tv)
        current = singer.get("/api/guest/tv-remote").json()
        assert current["available"] and current["online"]
        replacement = primary_tv_token(admin, replace=True)
        assert singer.get("/api/guest/tv-remote").json()["online"] is False
        assert command(singer, "pause", song).status_code == 409
        report(tv, replacement, active_id=song)
        assert command(singer, "pause", song).status_code == 200
        # User cookie for this event cannot control songs after the current one changes.
        with app.state.store.lock:
            app.state.store.db.execute(
                "UPDATE songs SET status='played' WHERE id=?", (song,)
            )
            another = setup_song(
                app, guest_id(app, "Singer B"), source_name="other.mp4"
            )
            app.state.store.db.execute(
                "UPDATE songs SET status='playing' WHERE id=?", (another,)
            )
            app.state.store.db.commit()
        assert singer.get("/api/guest/tv-remote").json() == {"available": False}
        assert command(singer, "pause", song).status_code == 403
        assert command(other, "skip", song).status_code == 409
        # Old primary TV credential cannot regain authority.
        assert tv.get("/api/tv/state", params={"token": token}).status_code == 401
        with app.state.store.lock:
            app.state.store.db.execute(
                "INSERT INTO room_state(name,value) VALUES('playback_mode','browser') "
                "ON CONFLICT(name) DO UPDATE SET value='browser'"
            )
            app.state.store.db.commit()
        assert other.get("/api/guest/tv-remote").json() == {"available": False}
        assert command(other, "skip", another).status_code == 409


def test_same_song_concurrent_guest_commands_are_single_flight(tmp_path):
    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv,
    ):
        song, token = setup_clients(app, admin, singer, other, tv)
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(
                executor.map(
                    lambda _index: command(singer, "restart", song).status_code,
                    range(5),
                )
            )
        assert results.count(200) == 1
        assert results.count(409) == 4
        assert (
            tv.get("/api/tv/state", params={"token": token}).json()["remote_command"][
                "action"
            ]
            == "restart"
        )


def test_admin_wins_concurrent_guest_admin_commands(tmp_path):
    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv,
    ):
        song, token = setup_clients(app, admin, singer, other, tv)
        with ThreadPoolExecutor(max_workers=2) as executor:
            guest_future = executor.submit(command, singer, "skip", song)
            admin_future = executor.submit(
                admin.post,
                "/api/admin/tv/remote",
                json={"action": "pause", "song_id": song},
            )
            guest_result = guest_future.result()
            admin_result = admin_future.result()
        assert guest_result.status_code in (200, 409)
        assert admin_result.status_code == 200
        dispatched = tv.get("/api/tv/state", params={"token": token}).json()[
            "remote_command"
        ]
        assert dispatched["action"] == "pause"


def test_guest_cookie_cannot_cross_events(tmp_path):
    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv,
    ):
        song, _ = setup_clients(app, admin, singer, other, tv)
        assert singer.get("/api/guest/tv-remote").json()["song_id"] == song
        app.state.events.close()
        app.state.events.create("New session", "654321")
        assert singer.get("/api/guest/tv-remote").status_code == 401
        assert command(singer, "skip", song).status_code == 401


def test_guest_sse_uses_shared_notifications_and_revokes_on_song_change(tmp_path):
    import asyncio

    from fastapi import HTTPException

    from vc_ktv.api.tv import create_tv_router

    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv_client,
    ):
        song, _ = setup_clients(app, admin, singer, other, tv_client)
        router = create_tv_router(
            tv=app.state.tv_session,
            playback=app.state.playback,
            media=app.state.media,
        )
        route = next(
            route for route in router.routes if route.path == "/api/guest/tv/events"
        )
        assert other.get("/api/guest/tv/events").status_code == 403

        class RequestStub:
            async def is_disconnected(self):
                return False

        async def check_stream():
            with pytest.raises(HTTPException) as denied:
                await route.endpoint(
                    RequestStub(), vc_guest=other.cookies.get("vc_guest")
                )
            assert denied.value.status_code == 403

            response = await route.endpoint(
                RequestStub(), vc_guest=singer.cookies.get("vc_guest")
            )
            stream = response.body_iterator
            try:
                assert (await asyncio.wait_for(anext(stream), 2)).startswith(
                    "event: ready"
                )
                # Same notification channel as admin remote; no extra bus.
                app.state.tv_session.notifications.notify("admin")
                assert (await asyncio.wait_for(anext(stream), 2)).startswith(
                    "event: update"
                )
                with app.state.store.lock:
                    app.state.store.db.execute(
                        "UPDATE songs SET status='played' WHERE id=?", (song,)
                    )
                    app.state.store.db.commit()
                app.state.tv_session.notifications.notify("admin")
                assert (await asyncio.wait_for(anext(stream), 2)).startswith(
                    "event: revoked"
                )
            finally:
                await stream.aclose()

        asyncio.run(check_stream())


def test_guest_command_wakes_shared_admin_and_guest_sse_channel(tmp_path):
    from unittest.mock import patch

    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv_client,
    ):
        song, _ = setup_clients(app, admin, singer, other, tv_client)
        with patch.object(app.state.tv_session.notifications, "notify") as notify:
            result = command(singer, "pause", song)
        assert result.status_code == 200
        assert [call.args[0] for call in notify.call_args_list] == ["tv", "admin"]


def test_guest_progress_and_offset_are_scoped_to_reported_song(tmp_path):
    app = room(tmp_path)
    with (
        TestClient(app) as admin,
        TestClient(app) as singer,
        TestClient(app) as other,
        TestClient(app) as tv,
    ):
        song, token = setup_clients(app, admin, singer, other, tv)
        assert command(singer, "offset", song, 1).status_code == 409
        report(tv, token, active_id=song, overlay=True, offset=-10, duration=25)
        state = singer.get("/api/guest/tv-remote").json()
        assert (
            state["position"],
            state["duration"],
            state["offset"],
            state["overlay"],
        ) == (15, 25, -10, True)
        assert other.get("/api/guest/tv-remote").json() == {"available": False}
        assert command(other, "offset", song, 10).status_code == 403
        assert command(singer, "offset", song + 1, 10).status_code == 409
        assert command(singer, "offset", song, 601).status_code == 400
        assert command(singer, "offset", song, -601).status_code == 400
        response = command(singer, "offset", song, 1)
        assert response.status_code == 200
        assert tv.get("/api/tv/state", params={"token": token}).json()[
            "remote_command"
        ] == {
            "seq": response.json()["seq"],
            "song_id": song,
            "action": "offset",
            "value": 1,
        }
        takeover = admin.post(
            "/api/admin/tv/remote", json={"action": "offset", "value": -5}
        )
        assert takeover.status_code == 200
        assert command(singer, "offset", song, 10).status_code == 409
        report(
            tv,
            token,
            ack_seq=takeover.json()["seq"],
            active_id=song,
            overlay=True,
            offset=-5,
            duration=25,
        )
        assert singer.get("/api/guest/tv-remote").json()["offset"] == -5
        # Stale telemetry from the previous song must not expose its timing or allow edits.
        report(tv, token, active_id=song + 1, overlay=True, offset=50, duration=200)
        state = singer.get("/api/guest/tv-remote").json()
        assert not state["online"] and not state["overlay"]
        assert (
            state["position"] is None
            and state["duration"] is None
            and state["offset"] is None
        )
        assert command(singer, "offset", song, 10).status_code == 409
