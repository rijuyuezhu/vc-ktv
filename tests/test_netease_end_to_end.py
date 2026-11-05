"""A real FFmpeg / subtitle / playback path, but with original synthetic audio."""

import shutil
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.lyric_selection import _candidate
from vc_ktv.netease_media import (
    MusicUnavailable,
    assemble_album_video,
    download_limited,
    download_netease_video,
    fetch_audio_url,
    parse_audio_response,
    validate_media_url,
)

RAW = "[00:01.00]第一行测试歌词\n[00:05.00]第二行测试歌词"


def test_media_url_and_trial_validation():
    assert validate_media_url("https://m10.music.126.net/test.mp3")
    assert validate_media_url("http://m804.music.126.net/test.mp3")
    for url in (
        "http://m10.music.126.net.evil.org/test.mp3",
        "https://music.126.net.evil.test/test.mp3",
        "https://127.0.0.1/internal",
        "https://m10.music.126.net:9090/test.mp3",
    ):
        with pytest.raises(ValueError):
            validate_media_url(url)
    with pytest.raises(MusicUnavailable):
        parse_audio_response(42, {"data": [{"id": 42, "code": 200, "url": None}]})
    with pytest.raises(MusicUnavailable):
        parse_audio_response(
            42,
            {
                "data": [
                    {
                        "id": 42,
                        "code": 200,
                        "url": "https://m10.music.126.net/example.mp3",
                        "freeTrialInfo": {"start": 0, "end": 30},
                    }
                ]
            },
        )


def test_audio_url_uses_new_weapi_and_carries_login_cookie():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [
                    {
                        "id": 42,
                        "code": 200,
                        "url": "http://m804.music.126.net/authorized.mp3",
                        "freeTrialInfo": None,
                    }
                ],
            },
        )

    url = fetch_audio_url(
        42,
        cookies={"MUSIC_U": "valid_test_cookie"},
        transport=httpx.MockTransport(handler),
    )
    assert url == "http://m804.music.126.net/authorized.mp3"
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.host == "music.163.com"
    assert seen[0].url.path == "/weapi/song/enhance/player/url/v1"
    assert seen[0].headers["cookie"] == "MUSIC_U=valid_test_cookie"
    assert b"encSecKey=" in seen[0].content and b"params=" in seen[0].content


def test_netease_cdn_download_accepts_http_and_rejects_errors(tmp_path):
    def handler(request):
        assert request.url.host == "m804.music.126.net"
        return httpx.Response(
            200, headers={"content-type": "audio/mpeg"}, content=b"x" * 4096
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        out = tmp_path / "audio.mp3"
        download_limited(client, "http://m804.music.126.net/sample", out, 8192)
        assert out.stat().st_size == 4096
        with pytest.raises(MusicUnavailable, match="大小超过限制"):
            download_limited(client, "http://m804.music.126.net/sample", out, 100)

    def range_handler(request):
        assert request.headers["range"] == "bytes=0-"
        return httpx.Response(
            206,
            headers={
                "content-range": "bytes 0-4095/4096",
                "content-type": "audio/mpeg",
            },
            content=b"a" * 4096,
        )

    with httpx.Client(transport=httpx.MockTransport(range_handler)) as client:
        download_limited(client, "http://m804.music.126.net/sample", out, 8192)
        assert out.stat().st_size == 4096

    def incomplete(_request):
        return httpx.Response(
            206, headers={"content-range": "bytes 0-1023/8192"}, content=b"a" * 1024
        )

    with httpx.Client(transport=httpx.MockTransport(incomplete)) as client:
        with pytest.raises(MusicUnavailable, match="部分媒体"):
            download_limited(client, "http://m804.music.126.net/sample", out, 8192)

    for code in (403, 404):
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(code))
        ) as client:
            with pytest.raises(MusicUnavailable, match=f"HTTP {code}"):
                download_limited(
                    client,
                    "http://m804.music.126.net/sample",
                    tmp_path / "missing",
                    8192,
                )


def test_download_tries_next_official_quality_on_cdn_rejection(tmp_path, monkeypatch):
    from vc_ktv import netease_media

    levels = []

    def fake_resolve(song_id, *, cookies, level):
        assert song_id == 42 and cookies == {"MUSIC_U": "test-session"}
        levels.append(level)
        return "http://m804.music.126.net/" + level

    def fake_download(client, url, path, limit):
        assert limit == netease_media.MAX_AUDIO_BYTES
        if url.endswith("/exhigh"):
            raise MusicUnavailable("网易云媒体下载失败（HTTP 403）")
        path.write_bytes(b"audio" * 700)

    def fake_render(audio, cover, destination, actual):
        assert actual == 90 and cover is None and audio.is_file()
        destination.write_bytes(b"video" * 700)
        return destination

    monkeypatch.setattr(netease_media, "fetch_audio_url", fake_resolve)
    monkeypatch.setattr(netease_media, "download_limited", fake_download)
    monkeypatch.setattr(netease_media, "_audio_duration", lambda _p: 90)
    monkeypatch.setattr(netease_media, "assemble_album_video", fake_render)
    result = download_netease_video(
        42, "", tmp_path, duration=87, cookies={"MUSIC_U": "test-session"}
    )
    assert levels == ["exhigh", "higher"]
    assert result.read_bytes().startswith(b"video")


def test_real_ffmpeg_netease_song_to_karaoke_queue_and_tv(tmp_path):
    """Prepare a NetEase-shaped song with a truly decodable synthetic audio file."""
    audio = tmp_path / "sample.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=8",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    app = create_app(
        root=tmp_path / "room",
        password="test-global",
        secret=b"netease-e2e",
        auto_prepare=True,
        gpu_alignment=False,
    )
    app.state.events.create("NetEase Test", "123456")

    async def lookup(song_id):
        assert song_id == 28391863
        return {
            "source": "netease",
            "source_id": str(song_id),
            "title": "测试歌曲",
            "uploader": "测试作者",
            "cover": "",
            "duration": 8,
            "official_url": "https://music.163.com/#/song?id=28391863",
        }

    async def search(_query):
        return [
            _candidate(
                "测试歌曲", "网易云音乐", RAW, "https://music.163.com/song?id=28391863"
            )
        ]

    seen = []

    def fake_download(song_id, cover, target, *, duration, cookies):
        assert song_id == 28391863 and duration == 8 and cover == "" and cookies is None
        seen.append(song_id)
        path = target / "licensed-synthetic-audio.wav"
        shutil.copyfile(audio, path)
        return assemble_album_video(path, None, target / "source.mp4", duration)

    app.state.netease_lookup = lookup
    app.state.candidate_search = search

    async def direct_lyrics(_id, *, title):
        assert _id == 28391863 and title == "测试歌曲"
        return await search(title)

    app.state.netease_lyrics = direct_lyrics
    app.state.autoprep.netease_download = fake_download
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "Music singer")
        assert (
            admin.post("/api/admin/login", json={"password": "test-global"}).status_code
            == 200
        )
        response = guest.post(
            "/api/netease/songs",
            json={"ref": "https://music.163.com/#/song?id=28391863"},
        )
        assert response.status_code == 200, response.text
        song_id = response.json()["id"]
        state = guest.get("/api/state").json()
        waiting = [s for s in state["waiting"] if s["id"] == song_id][0]
        assert waiting["source_type"] == "netease"
        assert waiting["source_id"] == "28391863"
        assert not waiting["bvid"]
        candidate = guest.get(f"/api/songs/{song_id}/lyrics/candidates").json()[
            "candidates"
        ][0]
        chosen = {
            "text": candidate["text"],
            "source": candidate["source"],
            "ticket": candidate["ticket"],
            "timing_mode": "provider",
            "timed_lrc": candidate["timed_lrc"],
            "source_lrc_raw": candidate["source_lrc_raw"],
            "timing_ticket": candidate["timing_ticket"],
        }
        selected = guest.post(f"/api/songs/{song_id}/lyrics/select", json=chosen)
        assert selected.status_code == 200, selected.text
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            row = app.state.events.current_store.rows(
                "SELECT * FROM songs WHERE id=?", (song_id,)
            )[0]
            if row["prep_state"] == "done":
                break
            time.sleep(0.2)
        assert seen == [28391863]
        assert row["status"] == "ready" and row["prep_state"] == "done", row
        assert row["source_type"] == "netease" and row["source_id"] == "28391863"
        detail = guest.get(f"/api/songs/{song_id}/detail").json()
        assert detail["source_type"] == "netease" and detail["source_id"] == "28391863"
        assert row["lyric_timing_mode"] == "provider"
        media = app.state.events.current_store.media / row["media_file"]
        assert media.exists() and media.stat().st_size > 1024
        media_info = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media),
            ],
            text=True,
        )
        assert "h264" in media_info and "aac" in media_info
        assert guest.get(f"/api/songs/{song_id}/download").status_code == 200
        assert admin.post("/api/admin/next").json()["active"]["id"] == song_id
        assert (
            admin.post("/api/admin/finish", json={"song_id": song_id}).status_code
            == 200
        )
        played = admin.get("/api/admin/played").json()["songs"][0]
        assert played["id"] == song_id
        assert (played["source_type"], played["source_id"]) == ("netease", "28391863")
        guest_history = guest.get("/api/songs/played").json()["songs"][0]
        assert (guest_history["source_type"], guest_history["source_id"]) == (
            "netease",
            "28391863",
        )
