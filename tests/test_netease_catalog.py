"""Offline tests for metadata preview: no NetEase account, audio or live HTTP."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.netease_catalog import lookup_netease_song, normalize_song_metadata


def sample_song(song_id=28391863):
    return {
        "code": 200,
        "songs": [
            {
                "id": song_id,
                "name": "示例歌名",
                "duration": 186200,
                "artists": [{"name": "歌手甲"}, {"name": "歌手乙"}],
                "album": {"picUrl": "http://p4.music.126.net/cover.jpg?param=300y300"},
            }
        ],
    }


def test_normalize_public_metadata_has_no_stream_url():
    info = normalize_song_metadata(28391863, sample_song())
    assert info == {
        "source": "netease",
        "source_id": "28391863",
        "title": "示例歌名",
        "uploader": "歌手甲 / 歌手乙",
        "duration": 187,
        "cover": "https://p4.music.126.net/cover.jpg?param=300y300",
        "official_url": "https://music.163.com/#/song?id=28391863",
    }
    assert not any("stream" in key or "audio" in key for key in info)


def test_public_metadata_rejects_bad_cover_or_unexpected_song_id():
    payload = sample_song()
    payload["songs"][0]["album"]["picUrl"] = "https://evil.example/audio.mp3"
    assert normalize_song_metadata(28391863, payload)["cover"] == ""
    with pytest.raises(LookupError):
        normalize_song_metadata(123, payload)
    payload["songs"][0]["duration"] = 0
    with pytest.raises(ValueError):
        normalize_song_metadata(28391863, payload)


def test_catalog_fetch_uses_fixed_host_only():
    requested = []

    def respond(request):
        requested.append((request.url.host, request.url.path, str(request.url.params)))
        return httpx.Response(200, json=sample_song())

    transport = httpx.MockTransport(respond)
    result = asyncio.run(lookup_netease_song(28391863, transport=transport))
    assert result["title"] == "示例歌名"
    assert len(requested) == 1
    assert requested[0][0:2] == ("music.163.com", "/api/song/detail/")
    assert "28391863" in requested[0][2]


def test_preview_auth_source_and_failures(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="global-pass",
        secret=b"metadata-test",
        auto_prepare=False,
    )
    seen_ids = []

    async def fake_lookup(song_id):
        seen_ids.append(song_id)
        return normalize_song_metadata(song_id, sample_song(song_id))

    app.state.netease_lookup = fake_lookup
    with (
        TestClient(app) as admin,
        TestClient(app) as guest,
        TestClient(app) as stranger,
    ):
        endpoint = "/api/netease/preview"
        valid = {"ref": "https://music.163.com/#/song?id=28391863"}
        assert stranger.get(endpoint, params=valid).status_code == 401
        assert (
            admin.post("/api/admin/login", json={"password": "global-pass"}).status_code
            == 200
        )
        assert admin.get(endpoint, params=valid).status_code == 200
        assert (
            admin.post(
                "/api/admin/events", json={"name": "Preview", "code": "123456"}
            ).status_code
            == 200
        )
        join_and_register(guest, "preview-tester")
        result = guest.get(endpoint, params=valid)
        assert result.status_code == 200
        assert result.json()["source_id"] == "28391863"
        assert seen_ids == [28391863, 28391863]
        assert (
            guest.get(
                endpoint, params={"ref": "https://attacker.test/song?id=28391863"}
            ).status_code
            == 400
        )
        assert seen_ids == [28391863, 28391863]

        async def missing(_):
            raise LookupError("missing")

        app.state.netease_lookup = missing
        assert guest.get(endpoint, params=valid).status_code == 404

        async def unavailable(_):
            raise httpx.ConnectError("offline")

        app.state.netease_lookup = unavailable
        assert guest.get(endpoint, params=valid).status_code == 502
