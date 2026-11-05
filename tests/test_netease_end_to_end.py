"""A real FFmpeg / subtitle / playback path, but with original synthetic audio."""

import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.lyric_selection import _candidate
from vc_ktv.netease_media import (
    MusicUnavailable,
    assemble_album_video,
    parse_audio_response,
    validate_media_url,
)

RAW = "[00:01.00]第一行测试歌词\n[00:05.00]第二行测试歌词"


def test_media_url_and_trial_validation():
    assert validate_media_url("https://m10.music.126.net/test.mp3")
    for url in (
        "http://m10.music.126.net/test.mp3",
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
