"""Search and share parsing are bounded, authenticated and offline-testable."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.netease_discovery import (
    extract_song_reference,
    resolve_song_reference,
    search_netease_songs,
)

SHARE = "分享洛天依Official/夜桜然的单曲《心事搁浅》: https://163cn.tv/bkjUiRgx (来自@网易云音乐)"
SONG_ID = 2613869782


def test_extract_song_from_share_text_and_official_links():
    assert extract_song_reference(SHARE) == "https://163cn.tv/bkjUiRgx"
    assert asyncio.run(resolve_song_reference(" 2613869782 ")) == SONG_ID
    assert (
        asyncio.run(
            resolve_song_reference(
                "分享歌曲: https://music.163.com/#/song?id=2613869782  (网易云音乐)"
            )
        )
        == SONG_ID
    )


def test_short_share_resolves_only_trusted_one_hop():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={
                "Location": "https://y.music.163.com/m/song?id=2613869782&share=1"
            },
        )

    song_id = asyncio.run(
        resolve_song_reference(SHARE, transport=httpx.MockTransport(handler))
    )
    assert song_id == SONG_ID
    assert urls == ["https://163cn.tv/bkjUiRgx"]


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/song?id=2613869782",
        "http://127.0.0.1/admin",
        "https://y.music.163.com/playlist?id=2613869782",
        "https://y.music.163.com/m/song?id=2613869782&id=123",
    ],
)
def test_short_share_does_not_follow_redirect_to_unsafe_location(location):
    def handler(_request):
        return httpx.Response(302, headers={"Location": location})

    with pytest.raises(ValueError):
        asyncio.run(
            resolve_song_reference(SHARE, transport=httpx.MockTransport(handler))
        )


@pytest.mark.parametrize(
    "text",
    [
        "https://163cn.tv.evil.example/bkjUiRgx",
        "http://163cn.tv/bkjUiRgx",
        "https://163cn.tv:8443/bkjUiRgx",
        "https://163cn.tv/bkjUiRgx?url=http://localhost",
        "https://163cn.tv/@another",
        "https://163cn.tv/bkjUiRgx https://evil.test/foo",
        "分享没有网址的歌曲",
    ],
)
def test_untrusted_share_rejected_without_network(text):
    with pytest.raises(ValueError):
        asyncio.run(resolve_song_reference(text))


def test_search_normalizes_results_and_uses_fixed_host():
    requested = []

    def handler(request):
        requested.append(request)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "result": {
                    "songs": [
                        {
                            "id": SONG_ID,
                            "name": "心事搁浅",
                            "duration": 198000,
                            "artists": [{"name": "洛天依Official"}, {"name": "夜桜然"}],
                            "album": {"picUrl": "https://p4.music.126.net/album.jpg"},
                        },
                        {"id": 123, "name": "无效时长", "duration": 0},
                    ]
                },
            },
        )

    songs = asyncio.run(
        search_netease_songs("心事搁浅", transport=httpx.MockTransport(handler))
    )
    assert [(s["source_id"], s["title"], s["uploader"]) for s in songs] == [
        (str(SONG_ID), "心事搁浅", "洛天依Official / 夜桜然")
    ]
    assert len(requested) == 1
    assert requested[0].url.host == "music.163.com"
    assert requested[0].url.path == "/api/search/get"
    assert requested[0].url.params["s"] == "心事搁浅"
    assert not any("stream" in s or "audio" in s for s in songs[0])


def test_empty_search_results_are_not_treated_as_provider_failure():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"code": 200, "result": {"songCount": 0}})
    )
    assert asyncio.run(search_netease_songs("没有", transport=transport)) == []


def test_search_requires_guest_and_allows_share_order(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test",
        secret=b"search-test",
        auto_prepare=False,
    )
    app.state.events.create("Search event", "123456")

    async def search(_query):
        return [
            {
                "source": "netease",
                "source_id": str(SONG_ID),
                "title": "心事搁浅",
                "uploader": "洛天依Official / 夜桜然",
                "cover": "",
                "duration": 198,
            }
        ]

    async def lookup(song_id):
        assert song_id == SONG_ID
        return (await search("x"))[0]

    async def resolve(_ref):
        return SONG_ID

    app.state.netease_search = search
    app.state.netease_lookup = lookup
    app.state.netease_resolve = resolve
    with TestClient(app) as guest, TestClient(app) as stranger:
        assert (
            stranger.get("/api/netease/search", params={"q": "心事搁浅"}).status_code
            == 401
        )
        join_and_register(guest, "搜索者")
        assert (
            guest.get("/api/netease/search", params={"q": "心事搁浅"}).json()["songs"][
                0
            ]["title"]
            == "心事搁浅"
        )
        assert guest.get("/api/netease/search", params={"q": " "}).status_code == 400
        assert guest.get("/api/netease/preview", params={"ref": SHARE}).json()[
            "source_id"
        ] == str(SONG_ID)
        response = guest.post("/api/netease/songs", json={"ref": SHARE})
        assert response.status_code == 200, response.text
        row = app.state.events.current_store.rows(
            "SELECT source_type,source_id FROM songs WHERE id=?",
            (response.json()["id"],),
        )[0]
        assert row == {"source_type": "netease", "source_id": str(SONG_ID)}
