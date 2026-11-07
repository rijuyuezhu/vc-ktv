"""Regression for HTTP origin checks and signed TV/media capability boundaries."""

from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register, primary_tv_token, use_browser_playback
from vc_ktv.application.factory import create_app


@pytest.fixture
def secured_app(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="secure-admin",
        secret=b"isolated-security-contract",
        auto_prepare=False,
    )
    app.state.events.create("Test Event", "123456")
    use_browser_playback(app)

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "Sample Song",
            "uploader": "Test",
            "cover": "",
            "duration": 60,
        }

    app.state.lookup = lookup
    app.state.probe = lambda _: 60
    return app


def test_cross_origin_writes_never_change_admin_settings_or_event(secured_app):
    app = secured_app
    with TestClient(app) as admin:
        assert (
            admin.post(
                "/api/admin/login", json={"password": "secure-admin"}
            ).status_code
            == 200
        )
        before = admin.get("/api/admin/settings").json()
        event_id = app.state.events.active_id
        for method, url, json in (
            (
                "PUT",
                "/api/admin/settings",
                {"queue_policy": "fcfs", "allow_unsubtitled": True},
            ),
            ("POST", "/api/admin/events/close", None),
            ("POST", "/api/admin/next", None),
        ):
            response = admin.request(
                method,
                url,
                json=json,
                headers={"Origin": "https://untrusted.example"},
            )
            assert response.status_code == 403
        # A sandboxed/file-page Origin is not an authorized same-origin caller.
        assert (
            admin.put(
                "/api/admin/settings",
                json={"queue_policy": "fcfs", "allow_unsubtitled": True},
                headers={"Origin": "null"},
            ).status_code
            == 403
        )
        assert admin.get("/api/admin/settings").json() == before
        assert app.state.events.active_id == event_id

        # A same-origin write is still accepted.
        assert (
            admin.put(
                "/api/admin/settings",
                json={"queue_policy": "fcfs", "allow_unsubtitled": True},
                headers={"Origin": "http://testserver"},
            ).status_code
            == 200
        )
        assert admin.get("/api/admin/settings").json()["queue_policy"] == "fcfs"


def test_tv_and_media_signatures_cannot_authorize_other_capabilities(secured_app):
    app = secured_app
    with TestClient(app) as admin, TestClient(app) as guest:
        assert (
            admin.post(
                "/api/admin/login", json={"password": "secure-admin"}
            ).status_code
            == 200
        )
        join_and_register(guest, "Guest")
        song_id = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        assert (
            admin.post(
                f"/api/admin/songs/{song_id}/media",
                files={"file": ("clip.mp4", b"sample-media", "video/mp4")},
            ).status_code
            == 200
        )
        tv_token = primary_tv_token(admin)
        active = admin.post("/api/admin/next").json()["active"]
        assert active["id"] == song_id
        media_path = active["media_url"]
        media_token = urlsplit(media_path).query.split("token=")[1]
        assert guest.get(media_path).content == b"sample-media"
        assert guest.get("/api/tv/state", params={"token": tv_token}).status_code == 200

        # Same event, different song: a valid media token is not transferable.
        other_id = guest.post("/api/songs", json={"bvid": "BV0000000002"}).json()["id"]
        assert other_id != song_id
        assert (
            admin.post(
                f"/api/admin/songs/{other_id}/media",
                files={"file": ("other.mp4", b"other-media", "video/mp4")},
            ).status_code
            == 200
        )
        assert (
            guest.get(f"/media/{other_id}", params={"token": media_token}).status_code
            == 403
        )

        # An otherwise valid token for one purpose never grants another purpose.
        assert (
            guest.get("/api/tv/state", params={"token": media_token}).status_code == 401
        )
        assert (
            guest.post(
                "/api/tv/finish",
                params={"token": media_token},
                json={"song_id": song_id},
            ).status_code
            == 401
        )
        assert (
            guest.get(f"/media/{song_id}", params={"token": tv_token}).status_code
            == 403
        )
        assert (
            guest.get(
                f"/api/tv/subtitles/{song_id}", params={"token": media_token}
            ).status_code
            == 403
        )
        assert (
            guest.get("/api/admin/settings", params={"token": tv_token}).status_code
            == 401
        )

        # Corrupted signature cannot advance or finish playback.
        corrupted = tv_token[:-1] + ("0" if tv_token[-1] != "0" else "1")
        assert (
            guest.post("/api/tv/next", params={"token": corrupted}).status_code == 401
        )
        assert (
            guest.post(
                "/api/tv/finish",
                params={"token": corrupted},
                json={"song_id": song_id},
            ).status_code
            == 401
        )
        assert app.state.store.current()["id"] == song_id
        assert app.state.store.rows("SELECT plays FROM guests")[0]["plays"] == 0
