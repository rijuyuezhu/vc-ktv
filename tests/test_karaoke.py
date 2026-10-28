import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from vc_ktv.app import create_app, probe_media
from vc_ktv.karaoke import parse_lrc, render_video, write_ass

LRC = "[00:01.00]<00:01.00>你<00:01.40>好\n[00:02.40]下一句"


def test_lrc_modes(tmp_path):
    lines = parse_lrc(LRC)
    assert len(lines) == 2 and lines[0]["units"][0] == (1.0, "你")
    report = write_ass(LRC, tmp_path / "sample.ass", duration=4, lead=0.8)
    content = (tmp_path / "sample.ass").read_text(encoding="utf-8-sig")
    assert report["timed_units"] == 2
    assert report["line_only"] == 1
    assert "{\\kf40}你" in content
    assert "{\\1c&H00FFFFFF&}下一句" in content
    assert "0:00:00.20" in content
    assert "0:00:01.00" not in content.split("Dialogue:")[1].split("{\\kf")[0]
    with pytest.raises(ValueError, match="时间轴"):
        parse_lrc("没时间戳")
    with pytest.raises(ValueError):
        write_ass(LRC, tmp_path / "invalid.ass", 4, 8)
    with pytest.raises(ValueError, match="递增"):
        parse_lrc("[00:00.50]<00:00.70>前<00:00.50>后")


@pytest.fixture
def sample_mp4(tmp_path):
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x182a38:s=480x270:r=15:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        timeout=15,
    )
    return path


def test_real_render_and_probe(sample_mp4, tmp_path):
    assert 3.9 < probe_media(sample_mp4) < 4.2
    output = tmp_path / "ktv.mp4"
    report = render_video(sample_mp4, LRC, output, duration=4, lead=1.8)
    assert report["timed_units"] == 2 and report["lead"] == 1.8
    assert output.stat().st_size > 5000
    data = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]
        )
    )
    assert {stream["codec_type"] for stream in data["streams"]} == {"audio", "video"}
    assert (
        next(s["codec_name"] for s in data["streams"] if s["codec_type"] == "video")
        == "h264"
    )


def test_app_real_render(sample_mp4, tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test",
        secret=b"test-secret",
        auto_prepare=False,
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bv, page):
        return {
            "bvid": bv,
            "page": page,
            "title": "可用歌曲",
            "uploader": "example",
            "cover": "",
            "duration": 4,
            "pages": [{"page": 1, "part": "main"}],
            "official_url": "",
            "embed_url": "",
        }

    app.state.lookup = lookup
    with TestClient(app) as user, TestClient(app) as admin:
        song = user.post(
            "/api/songs", json={"bvid": "BV0000000003", "page": 1, "nickname": "测试"}
        ).json()["id"]
        assert (
            admin.post("/api/admin/login", json={"password": "test"}).status_code == 200
        )
        assert (
            admin.post(
                f"/api/admin/songs/{song}/media",
                files={"file": ("valid.mp4", sample_mp4.read_bytes(), "video/mp4")},
            ).status_code
            == 200
        )
        assert (
            admin.post(
                f"/api/admin/songs/{song}/render", json={"lead": 1.5}
            ).status_code
            == 400
        )
        assert (
            admin.post(
                f"/api/admin/songs/{song}/lyrics", json={"text": LRC}
            ).status_code
            == 200
        )
        assert (
            admin.post(
                f"/api/admin/songs/{song}/render", json={"lead": 1.5}
            ).status_code
            == 200
        )
        for _ in range(100):
            state = admin.get("/api/admin/state").json()
            row = next(s for s in state["queue"] + state["waiting"] if s["id"] == song)
            if row["status"] == "ready":
                break
            time.sleep(0.1)
        assert row["status"] == "ready" and row["render_error"] == ""
        active = admin.post("/api/admin/next").json()["active"]
        assert active and active["id"] == song
        result = admin.get(active["media_url"])
        assert result.status_code == 200 and len(result.content) > 5000
        assert result.headers["content-type"] == "video/mp4"


def test_invalid_upload_is_rejected(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test",
        secret=b"test-secret",
        auto_prepare=False,
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bv, page):
        return {
            "bvid": bv,
            "page": page,
            "title": "测试",
            "uploader": "example",
            "cover": "",
            "duration": 4,
        }

    app.state.lookup = lookup
    with TestClient(app) as user, TestClient(app) as admin:
        song = user.post(
            "/api/songs", json={"bvid": "BV0000000003", "nickname": "测试"}
        ).json()["id"]
        admin.post("/api/admin/login", json={"password": "test"})
        res = admin.post(
            f"/api/admin/songs/{song}/media",
            files={"file": ("bad.mp4", b"not a video", "video/mp4")},
        )
        assert res.status_code == 400
        assert admin.get("/api/state").json()["waiting"][0]["status"] == "waiting"


def test_render_with_relative_file_paths(sample_mp4, tmp_path, monkeypatch):
    """FFmpeg runs in a temp directory, but media paths may be relative."""
    monkeypatch.chdir(tmp_path)
    from pathlib import Path

    report = render_video(
        Path("source.mp4"), LRC, Path("relative-result.mp4"), duration=4, lead=1.1
    )
    assert report["lines"] == 2
    assert (tmp_path / "relative-result.mp4").stat().st_size > 5000
