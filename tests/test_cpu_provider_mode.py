"""CPU mode keeps BV preparation and source-timed rendering without any GPU."""

import shutil
import subprocess
import time

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.application.factory import create_app
from vc_ktv.lyrics.lyric_selection import _candidate

RAW = "[00:01.00]第一行正确歌词\n[00:05.00]第二行正确歌词"


def test_cpu_only_auto_download_and_provider_render(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"cpu-mode-alignment",
        auto_prepare=True,
        gpu_alignment=False,
    )
    app.state.events.create("CPU Mode", "123456")
    sample = tmp_path / "sample.mp4"
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
            str(sample),
        ],
        check=True,
        capture_output=True,
    )

    async def lookup(bvid, page):
        return dict(
            bvid=bvid, page=page, title="测试歌曲", uploader="UP", cover="", duration=12
        )

    async def search(query, phase="all"):
        return [_candidate("测试歌曲", "网易云音乐", RAW, "https://example.org/lyrics")]

    app.state.lookup = lookup
    app.state.candidate_search = search
    worker = app.state.autoprep

    def download(bvid, page, directory):
        out = directory / "source.mp4"
        shutil.copyfile(sample, out)
        return out

    worker.download = download
    worker.probe = lambda path: 12
    worker.enhance = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("GPU must not be invoked")
    )
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["gpu_alignment"] is False
        assert health["autoprep_ready"] is True
        worker.stop()
        assert client.get("/api/health").json()["autoprep_ready"] is False
        until = time.monotonic() + 15
        while (
            time.monotonic() < until
            and not client.get("/api/health").json()["autoprep_ready"]
        ):
            time.sleep(0.25)
        assert client.get("/api/health").json()["autoprep_workers"] == 1
        join_and_register(client, "CPU tester")
        song_id = client.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        row = client.get(f"/api/songs/{song_id}/lyrics/candidates").json()[
            "candidates"
        ][0]
        choice = dict(source=row["source"], text=row["text"], ticket=row["ticket"])
        assert (
            client.post(f"/api/songs/{song_id}/lyrics/select", json=choice).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/songs/{song_id}/lyrics/upload",
                files={"file": ("words.lrc", RAW.encode(), "text/plain")},
            ).status_code
            == 409
        )
        choice.update(
            timing_mode="provider",
            timed_lrc=row["timed_lrc"],
            source_lrc_raw=row["source_lrc_raw"],
            timing_ticket=row["timing_ticket"],
            offset_ms=0,
        )
        response = client.post(f"/api/songs/{song_id}/lyrics/select", json=choice)
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            saved = app.state.events.current_store.rows(
                "SELECT status,prep_state,media_file FROM songs WHERE id=?", (song_id,)
            )[0]
            if saved["prep_state"] == "done":
                break
            time.sleep(0.2)
        assert saved["status"] == "ready" and saved["prep_state"] == "done", saved
        assert (app.state.events.current_store.media / saved["media_file"]).is_file()
        assert client.get(f"/api/songs/{song_id}/download").status_code == 200


def test_cpu_mode_can_still_choose_original(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"cpu-mode-original",
        auto_prepare=False,
        gpu_alignment=False,
    )
    app.state.events.create("CPU Mode", "123456")

    async def lookup(bvid, page):
        return dict(
            bvid=bvid, page=page, title="测试歌曲", uploader="UP", cover="", duration=12
        )

    app.state.lookup = lookup
    with TestClient(app) as client:
        join_and_register(client, "CPU tester")
        song_id = client.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        r = client.post(f"/api/songs/{song_id}/lyrics/skip")
        assert r.status_code == 200
        row = app.state.events.current_store.rows(
            "SELECT skip_subtitles,lyric_choice FROM songs WHERE id=?", (song_id,)
        )[0]
        assert row["skip_subtitles"] == 1 and row["lyric_choice"] == ""
