"""The selected NetEase song ID takes precedence over ambiguous title search."""

import asyncio

import httpx
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.application.factory import create_app
from vc_ktv.lyric_selection import _candidate, netease_song_lyrics

SONG = 3434373208
RAW = "[00:01.00]第一句歌词\n[00:05.00]第二句歌词"


def test_exact_lyrics_uses_official_song_id_without_searching():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"code": 200, "lrc": {"lyric": RAW}})

    result = asyncio.run(
        netease_song_lyrics(
            SONG, title="沦为尘埃", transport=httpx.MockTransport(respond)
        )
    )
    assert len(result) == 1
    assert result[0]["title"] == "沦为尘埃"
    assert result[0]["source"] == "网易云音乐"
    assert result[0]["source_lrc_raw"] == RAW
    assert result[0]["timed_lrc"] == RAW
    assert len(seen) == 1
    assert seen[0].url.host == "music.163.com"
    assert seen[0].url.path == "/api/song/lyric"
    assert seen[0].url.params["id"] == str(SONG)


def test_exact_lyrics_missing_or_invalid_returns_no_candidate():
    def respond(_request):
        return httpx.Response(200, json={"code": 200, "nolyric": True})

    assert (
        asyncio.run(netease_song_lyrics(SONG, transport=httpx.MockTransport(respond)))
        == []
    )
    assert (
        asyncio.run(netease_song_lyrics(0, transport=httpx.MockTransport(respond)))
        == []
    )


def test_exact_lyrics_prefers_real_content_over_stale_nolyric_flag():
    def respond(_):
        return httpx.Response(
            200, json={"code": 200, "nolyric": True, "lrc": {"lyric": RAW}}
        )

    rows = asyncio.run(
        netease_song_lyrics(SONG, transport=httpx.MockTransport(respond))
    )
    assert rows[0]["timed_lrc"] == RAW


def test_guest_candidates_prefer_exact_id_and_keep_other_sources(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"exact-lyric-source",
        auto_prepare=False,
    )
    app.state.events.create("Test", "123456")

    async def lookup(song_id):
        assert song_id == SONG
        return {
            "source": "netease",
            "source_id": str(SONG),
            "title": "沦为尘埃",
            "uploader": "歌手甲",
            "cover": "",
            "duration": 180,
        }

    async def exact(song_id, *, title):
        assert song_id == SONG and title == "沦为尘埃"
        return [
            _candidate(
                title, "网易云音乐", RAW, f"https://music.163.com/song?id={song_id}"
            )
        ]

    async def names(_query):
        return [
            _candidate(
                "沦为尘埃（翻唱）",
                "网易云音乐",
                "[00:01.00]错误版本\n[00:02.00]错误第二句",
                "https://music.163.com/song?id=42",
            ),
            _candidate("沦为尘埃", "LRCLIB", RAW, "https://lrclib.net/api/get/55"),
        ]

    app.state.netease_lookup = lookup
    app.state.netease_lyrics = exact
    app.state.candidate_search = names
    with TestClient(app) as guest:
        join_and_register(guest, "歌手")
        song_id = guest.post("/api/netease/songs", json={"ref": str(SONG)}).json()["id"]
        songs = guest.get(f"/api/songs/{song_id}/lyrics/candidates").json()[
            "candidates"
        ]
        assert [s["source"] for s in songs] == ["网易云音乐", "网易云音乐", "LRCLIB"]
        assert songs[0]["title"] == "沦为尘埃"
        assert all(s["ticket"] for s in songs)
