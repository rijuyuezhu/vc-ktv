"""Provider timing is deliberately opt-in, authenticated, and line-level only."""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.lyric_selection import _candidate
from vc_ktv.timed_lyrics import offset_timeline, provider_timeline

RAW = (
    "[ar:Artist]\n[00:01.00]画师：Painter\n[00:02.00]策划/混音：Team\n"
    "[00:05.00]第一次演唱的歌词\n[00:10.00]第二次演唱的歌词"
)
TEXT = "第一次演唱的歌词\n第二次演唱的歌词"


def test_clean_credits_and_keep_signed_source_and_offset():
    candidate = _candidate(
        "歌曲", "网易云音乐", RAW, "https://music.163.com/song?id=11"
    )
    assert candidate["text"] == TEXT
    assert candidate["source_lrc_raw"] == RAW
    assert candidate["unfiltered_text"].startswith("画师：Painter")
    assert (
        candidate["timed_lrc"]
        == "[00:05.00]第一次演唱的歌词\n[00:10.00]第二次演唱的歌词"
    )
    assert offset_timeline(candidate["timed_lrc"], 2500, 80).startswith("[00:07.50]")
    assert offset_timeline(candidate["timed_lrc"], -1000, 80).endswith(
        "[00:09.00]第二次演唱的歌词"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "没有时间戳\n第二句也没有",
        "[00:01.00]一句歌词\n没有时间戳的第二句\n[00:05.00]第三句歌词",
        "[00:10.00]一句歌词\n[00:05.00]第二句歌词",
        "[00:01.00]一句歌词\n[00:01.00]第二句歌词",
        "[offset:1500]\n[00:01.00]一句歌词\n[00:05.00]第二句歌词",
        "[00:01.00]<00:01.00>一句歌词\n[00:05.00]第二句歌词",
    ],
)
def test_reject_untrustworthy_provider_timing(raw):
    from ktv_app.text_normalization import clean_lyric_text

    assert provider_timeline(raw, clean_lyric_text(raw)) is None


def test_reject_clipped_version_or_time_warp():
    timed = provider_timeline(RAW, TEXT)
    with pytest.raises(ValueError, match="超出视频"):
        offset_timeline(timed, -6000, 60)
    with pytest.raises(ValueError, match="超出视频"):
        offset_timeline(timed, 55_000, 60)
    with pytest.raises(ValueError, match="偏移"):
        offset_timeline(timed, 70_000, 60)
    with pytest.raises(ValueError, match="密集"):
        offset_timeline("[00:01.00]第一次演唱的歌词\n[00:01.02]第二次演唱的歌词", 0, 60)
    assert provider_timeline(RAW, TEXT + "\n错版新增的歌词") is None


def make_app(tmp_path, duration=60):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"provider-source",
        auto_prepare=False,
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bvid, page):
        return dict(
            bvid=bvid,
            page=page,
            title="示例歌曲",
            uploader="UP",
            cover="",
            duration=duration,
        )

    async def search(_query, _phase="all"):
        return [
            _candidate(
                "示例歌曲", "网易云音乐", RAW, "https://music.163.com/song?id=11"
            )
        ]

    app.state.lookup = lookup
    app.state.candidate_search = search
    return app


def test_guest_provider_choice_signed_and_default_unchanged(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as guest:
        join_and_register(guest, "歌手")
        song_id = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        candidate = guest.get(f"/api/songs/{song_id}/lyrics/candidates").json()[
            "candidates"
        ][0]
        route = f"/api/songs/{song_id}/lyrics/select"
        payload = dict(
            text=candidate["text"],
            source=candidate["source"],
            ticket=candidate["ticket"],
            timing_mode="provider",
            timed_lrc=candidate["timed_lrc"],
            source_lrc_raw=candidate["source_lrc_raw"],
            timing_ticket=candidate["timing_ticket"],
            offset_ms=2500,
        )
        assert (
            guest.post(
                route, json={**payload, "timed_lrc": payload["timed_lrc"] + " fake"}
            ).status_code
            == 403
        )
        assert (
            guest.post(route, json={**payload, "offset_ms": -8000}).status_code == 400
        )
        assert (
            guest.post(route, json={**payload, "timing_ticket": "invalid"}).status_code
            == 403
        )
        assert guest.post(route, json=payload).status_code == 200
        stored = app.state.store.rows(
            "SELECT lyric_choice,lyric_timing_mode,provider_lrc,source_lrc_raw,lyric_offset_ms "
            "FROM songs WHERE id=?",
            (song_id,),
        )[0]
        assert stored == {
            "lyric_choice": TEXT,
            "lyric_timing_mode": "provider",
            "provider_lrc": candidate["timed_lrc"],
            "source_lrc_raw": RAW,
            "lyric_offset_ms": 2500,
        }
        # Switching back to ordinary Kim/Qwen/TIFA also resets source timing.
        assert (
            guest.post(
                route,
                json={
                    "text": TEXT,
                    "source": candidate["source"],
                    "ticket": candidate["ticket"],
                },
            ).status_code
            == 200
        )
        assert app.state.store.rows(
            "SELECT lyric_timing_mode,provider_lrc,lyric_offset_ms FROM songs WHERE id=?",
            (song_id,),
        ) == [{"lyric_timing_mode": "align", "provider_lrc": "", "lyric_offset_ms": 0}]


def test_opt_in_preparation_bypasses_gpu_and_keeps_original_audio(tmp_path):
    app = make_app(tmp_path, duration=12)
    source = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=25:d=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=12",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    with TestClient(app) as guest:
        join_and_register(guest, "歌手")
        song_id = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        candidate = guest.get(f"/api/songs/{song_id}/lyrics/candidates").json()[
            "candidates"
        ][0]
        chosen = {
            "text": candidate["text"],
            "source": "网易云音乐",
            "ticket": candidate["ticket"],
            "timing_mode": "provider",
            "timed_lrc": candidate["timed_lrc"],
            "source_lrc_raw": candidate["source_lrc_raw"],
            "timing_ticket": candidate["timing_ticket"],
            "offset_ms": 1000,
        }
        assert (
            guest.post(f"/api/songs/{song_id}/lyrics/select", json=chosen).status_code
            == 200
        )
        store = app.state.store
        target_source = store.media / f"{song_id}-original.mp4"
        target_source.write_bytes(source.read_bytes())
        with store.lock:
            store.db.execute(
                "UPDATE songs SET status='ready',prep_state='downloaded',"
                "source_file=?,media_file=? WHERE id=?",
                (target_source.name, target_source.name, song_id),
            )
            store.db.commit()

        def forbidden(*args, **kwargs):
            raise AssertionError("provider line mode must not invoke model alignment")

        store.save_settings("fair", False, subtitle_style="compact")
        worker = app.state.autoprep
        worker.enhance = forbidden
        task = worker.claim()
        assert task["id"] == song_id and task["prep_state"] == "downloaded"
        worker.prepare_subtitles(task)
        new = store.rows(
            "SELECT prep_state,media_file,lyrics FROM songs WHERE id=?", (song_id,)
        )[0]
        assert new["prep_state"] == "done"
        video = store.media / new["media_file"]
        assert video.is_file() and video.stat().st_size > 1024
        assert new["lyrics"].startswith("[00:06.00]")

        def audio_md5(path):
            return subprocess.check_output(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-map",
                    "0:a",
                    "-c:a",
                    "copy",
                    "-f",
                    "md5",
                    "-",
                ],
                text=True,
            ).strip()

        assert audio_md5(video) == audio_md5(target_source)
        archive = next((store.media / f"{song_id}-auto-alignment").glob("attempt-*"))
        report = json.loads((archive / "provider_timing.json").read_text())
        assert report["mode"] == "provider_line_only"
        assert report["verified_against_audio"] is False
        ass = (archive / "provider_subtitles" / "lyrics.ass").read_text(
            encoding="utf-8-sig"
        )
        assert "第一" in ass
        assert "Noto Sans CJK SC,38," in ass  # Event subtitle preset still honored.
        assert (
            "\\kf" not in ass
        )  # Source LRC has only line timing; no invented word sweeps.
        assert guest.get(f"/api/songs/{song_id}/download").status_code == 200
        detail = guest.get(f"/api/songs/{song_id}/detail").json()
        assert (
            detail["lyric_timing_mode"] == "provider"
            and detail["lyric_offset_ms"] == 1000
        )
        with TestClient(app) as admin:
            assert (
                admin.post("/api/admin/login", json={"password": "pw"}).status_code
                == 200
            )
            active = admin.post("/api/admin/next").json()["active"]
            assert active["id"] == song_id
            assert (
                admin.post("/api/admin/finish", json={"song_id": song_id}).status_code
                == 200
            )
            replay = admin.post(f"/api/admin/songs/{song_id}/replay")
            assert replay.status_code == 200, replay.text
            new_id = replay.json()["id"]
            assert store.rows(
                "SELECT lyric_timing_mode,lyric_offset_ms,provider_lrc "
                "FROM songs WHERE id=?",
                (new_id,),
            ) == [
                {
                    "lyric_timing_mode": "provider",
                    "lyric_offset_ms": 1000,
                    "provider_lrc": candidate["timed_lrc"],
                }
            ]


def test_long_instrumental_intro_is_not_retimed_without_explicit_offset():
    original = "[01:30.00]第一次演唱的歌词\n[01:40.00]第二次演唱的歌词"
    canonical = provider_timeline(original, TEXT)
    assert canonical == original
    assert offset_timeline(canonical, -30_000, 150).startswith("[01:00.00]")
    # A video cut to 80s cannot take this source as-is.
    with pytest.raises(ValueError, match="超出视频"):
        offset_timeline(canonical, 0, 80)


def test_lrclib_prefers_synced_over_plain_when_available(monkeypatch):
    import asyncio

    import httpx

    from vc_ktv import lyric_selection

    async def handler(request):
        assert request.url.path.endswith("/api/search")
        return httpx.Response(
            200,
            json=[
                {
                    "id": 12,
                    "trackName": "第一首",
                    "artistName": "歌手",
                    "plainLyrics": TEXT,
                    "syncedLyrics": "[00:05.00]第一次演唱的歌词\n[00:10.00]第二次演唱的歌词",
                },
                {
                    "id": 13,
                    "trackName": "第二首",
                    "plainLyrics": TEXT,
                    "syncedLyrics": None,
                },
            ],
        )

    original = lyric_selection.httpx.AsyncClient
    monkeypatch.setattr(
        lyric_selection.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    rows = asyncio.run(lyric_selection._lrclib("第一首"))
    assert [x["title"] for x in rows] == ["第一首", "第二首"]
    assert rows[0]["timed_lrc"].startswith("[00:05.00]")
    assert "timed_lrc" not in rows[1]
