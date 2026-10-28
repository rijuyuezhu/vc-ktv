from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from textgrid import IntervalTier, TextGrid

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.karaoke import parse_lrc, write_ass
from vc_ktv.textgrid_import import convert_textgrid


def grid_file(path: Path, tokens=None):
    if tokens is None:
        tokens = [
            (0.5, 0.75, "你"),
            (0.9, 1.25, "好"),
            (1.6, 1.83, "下"),
            (1.84, 2.10, "一"),
            (2.2, 2.55, "句"),
        ]
    grid = TextGrid(minTime=0, maxTime=4)
    tier = IntervalTier(name="texts", minTime=0, maxTime=4)
    for start, end, word in tokens:
        tier.add(start, end, word)
    grid.append(tier)
    grid.write(str(path))
    return path


def test_strict_conversion_and_explicit_ends(tmp_path):
    path = grid_file(tmp_path / "test.TextGrid")
    enhanced, report = convert_textgrid(path, "你，好!\n下一句", duration=4)
    assert report["semantic_units"] == 5
    assert report["review_required"] is True
    assert enhanced.startswith(
        "[00:00.50]<00:00.50>你<00:00.75><00:00.90>，好!<00:01.25>"
    )
    assert enhanced.splitlines()[1].startswith("[00:01.60]")
    parsed = parse_lrc(enhanced)
    assert parsed[0]["ends"] == [0.75, 1.25]
    assert parsed[0]["text"] == "你，好!"
    report = write_ass(enhanced, tmp_path / "render.ass", duration=4, lead=0.4)
    assert report["timed_units"] == 5
    ass = (tmp_path / "render.ass").read_text(encoding="utf-8-sig")
    assert "{\\kf25}你" in ass
    assert "{\\k15}" in ass  # real pause between units, not extended highlight


def test_mismatch_and_invalid_time_rejected(tmp_path):
    grid = grid_file(tmp_path / "test.TextGrid")
    for bad in ["你好吗\n下一句", "你，好!\n下两句"]:
        with pytest.raises(ValueError, match="匹配|跨越"):
            convert_textgrid(grid, bad, 4)
    crossing = grid_file(tmp_path / "crossing.TextGrid", [(0.5, 1.0, "你好")])
    with pytest.raises(ValueError, match="跨越"):
        convert_textgrid(crossing, "你\n好", 4)
    with pytest.raises(ValueError, match="视频范围"):
        convert_textgrid(grid, "你，好!\n下一句", 1.0)
    with pytest.raises(ValueError):
        convert_textgrid(
            grid_file(
                tmp_path / "overlap.TextGrid", [(0.5, 1.0, "你"), (0.9, 1.3, "好")]
            ),
            "你好",
            4,
        )


def test_endpoint_saves_raw_and_rejects_bad_version(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pass",
        secret=b"test-secret",
        auto_prepare=False,
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "测试",
            "uploader": "测试",
            "cover": "",
            "duration": 4,
        }

    app.state.lookup = lookup
    app.state.probe = lambda file: 4
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "访客")
        song = guest.post(
            "/api/songs",
            json={
                "bvid": "BV0000000003",
                "nickname": "测试",
                "page": 1,
            },
        ).json()["id"]
        assert (
            admin.post("/api/admin/login", json={"password": "pass"}).status_code == 200
        )
        admin.post(
            f"/api/admin/songs/{song}/media",
            files={"file": ("source.mp4", b"test-dummy-media", "video/mp4")},
        )
        assert (
            admin.post(
                f"/api/admin/songs/{song}/lyrics",
                json={"text": "你，好!\n下一句"},
            ).status_code
            == 200
        )
        grid = grid_file(tmp_path / "import.TextGrid").read_bytes()
        denied = guest.post(
            f"/api/admin/songs/{song}/alignment/import",
            files={"file": ("alignment.TextGrid", grid, "text/plain")},
        )
        assert denied.status_code == 401
        ok = admin.post(
            f"/api/admin/songs/{song}/alignment/import",
            files={"file": ("alignment.TextGrid", grid, "text/plain")},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["report"]["semantic_units"] == 5
        assert (
            "<00:00.75>" in admin.get(f"/api/admin/songs/{song}/lyrics").json()["text"]
        )
        assert len(list(app.state.store.media.glob("*-alignment-*"))) == 1
        bad = admin.post(
            f"/api/admin/songs/{song}/alignment/import",
            files={"file": ("alignment.TextGrid", grid, "text/plain")},
        )
        assert bad.status_code == 400


def test_full_textgrid_to_rendered_mp4(tmp_path):
    import subprocess
    import time

    app = create_app(
        root=tmp_path / "room-real",
        password="pass",
        secret=b"test-secret",
        auto_prepare=False,
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "测试",
            "uploader": "测试",
            "cover": "",
            "duration": 4,
        }

    app.state.lookup = lookup
    movie = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x334455:s=320x180:r=15:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(movie),
        ],
        check=True,
        timeout=20,
    )
    grid = grid_file(tmp_path / "real.TextGrid").read_bytes()
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "访客")
        song = guest.post(
            "/api/songs", json={"bvid": "BV0000000003", "nickname": "测试", "page": 1}
        ).json()["id"]
        assert (
            admin.post("/api/admin/login", json={"password": "pass"}).status_code == 200
        )
        res = admin.post(
            f"/api/admin/songs/{song}/media",
            files={"file": ("source.mp4", movie.read_bytes(), "video/mp4")},
        )
        assert res.status_code == 200, res.text
        assert (
            admin.post(
                f"/api/admin/songs/{song}/lyrics", json={"text": "你，好!\n下一句"}
            ).status_code
            == 200
        )
        res = admin.post(
            f"/api/admin/songs/{song}/alignment/import",
            files={"file": ("aligned.TextGrid", grid, "text/plain")},
        )
        assert res.status_code == 200, res.text
        assert (
            admin.post(
                f"/api/admin/songs/{song}/render", json={"lead": 1.3}
            ).status_code
            == 200
        )
        for _ in range(100):
            state = admin.get("/api/admin/state").json()
            matches = [s for s in state["queue"] if s["id"] == song]
            if matches:
                assert not matches[0]["render_error"]
                break
            time.sleep(0.1)
        assert matches, "render did not finish"
        song_info = admin.post("/api/admin/next").json()["active"]
        response = admin.get(song_info["media_url"])
        assert response.status_code == 200
        output = tmp_path / "result.mp4"
        output.write_bytes(response.content)
        assert output.stat().st_size > 5000
        probe = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=nw=1",
                str(output),
            ],
            timeout=12,
            text=True,
        )
        assert "codec_name=h264" in probe
        assert "codec_name=aac" in probe


def test_real_tifa_generated_fixture_to_ktv_ass(tmp_path):
    """A real TIFA CUDA TextGrid from our original synthetic Mandarin speech."""
    fixture = Path(__file__).resolve().parent / "fixtures/tifa-espeak-hello.TextGrid"
    lrc, info = convert_textgrid(fixture, "你好世界", duration=2.263628)
    assert info["semantic_units"] == 4
    assert "<00:00.00>你<00:00.32>" in lrc
    assert "<00:01.21>界<00:01.65>" in lrc
    from vc_ktv.karaoke import write_ass

    result = write_ass(lrc, tmp_path / "verified.ass", 2.263628, 1.2)
    assert result["timed_units"] == 4
    text = (tmp_path / "verified.ass").read_text(encoding="utf-8-sig")
    assert r"{\kf32}你" in text
    assert r"{\kf44}好" in text


def test_scat_pronunciation_preserves_original_display_with_native_bounds(tmp_path):
    from vc_ktv.autoprep import pronunciation_lyrics

    original = "DUDUDU DaLaDa\n你好"
    spoken, mapping, changes = pronunciation_lyrics(original)
    assert spoken == "嘟嘟嘟 哒啦哒\n你好"
    assert len(changes) == 2
    grid = grid_file(
        tmp_path / "scat.TextGrid",
        [
            (0.10, 0.25, "嘟"),
            (0.25, 0.40, "嘟"),
            (0.40, 0.60, "嘟"),
            (0.75, 0.90, "哒"),
            (0.90, 1.05, "啦"),
            (1.05, 1.30, "哒"),
            (1.50, 1.70, "你"),
            (1.70, 1.90, "好"),
        ],
    )
    lrc, report = convert_textgrid(
        grid, original, 3, pronunciation=spoken, source_spans=mapping
    )
    parsed = parse_lrc(lrc)
    assert [line["text"] for line in parsed] == original.splitlines()
    assert parsed[0]["ends"] == [0.6, 1.3]
    assert len(parsed[0]["units"]) == 2
    assert parsed[0]["units"][0] == (0.1, "DUDUDU")
    assert parsed[0]["units"][1] == (0.75, " DaLaDa")
    assert report["semantic_units"] == 8
    assert report["verified_by_model"] is False
    assert write_ass(lrc, tmp_path / "scat.ass", 3)["timed_units"] == 4


def test_scat_mapping_rejects_unsafe_or_mismatched_positions(tmp_path):
    from vc_ktv.autoprep import pronunciation_lyrics

    original = "DUDUDU 你好"
    spoken, mapping, _ = pronunciation_lyrics(original)
    grid = grid_file(tmp_path / "mismatch.TextGrid", [(0.1, 0.9, "嘟嘟嘟你好")])
    wrong = [mapping[0][::-1]]
    with pytest.raises(ValueError, match="映射"):
        convert_textgrid(grid, original, 3, pronunciation=spoken, source_spans=wrong)
    with pytest.raises(ValueError, match="匹配"):
        convert_textgrid(grid, original, 3)


def test_auto_scat_pipeline_keeps_display_and_pronunciation_trace(
    tmp_path, monkeypatch
):
    import json
    import subprocess

    from vc_ktv import autoprep

    root = tmp_path / "model-fixture"
    for name in (
        ".runtime/TIFA/infer.py",
        ".runtime/tifa-env/bin/python",
        ".runtime/models/TIFA-1.0-ST/model.pt",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    monkeypatch.setattr(autoprep, "ROOT", root)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"unmodified-original")
    rendered = []

    def fake_run(command, **kwargs):
        if command[0] != "ffmpeg":
            folder = Path(command[command.index("-o") + 1])
            grid_file(
                folder / "audio.TextGrid",
                [
                    (0.1, 0.2, "嘟"),
                    (0.2, 0.3, "嘟"),
                    (0.3, 0.4, "嘟"),
                    (0.5, 0.7, "你"),
                    (0.7, 0.9, "好"),
                ],
            )
        return subprocess.CompletedProcess(command, 0)

    def fake_render(original, lrc, target, duration, lead):
        assert original.read_bytes() == b"unmodified-original"
        rendered.append(lrc)
        target.write_bytes(b"rendered")

    monkeypatch.setattr(autoprep.subprocess, "run", fake_run)
    monkeypatch.setattr(autoprep, "render_video", fake_render)
    archive = tmp_path / "attempt"
    report = autoprep.make_tifa_video(
        source, "DUDUDU\n你好", tmp_path / "output.mp4", archive, 4
    )
    assert report["pronunciation_substitutions"] == 1
    assert (archive / "lyrics.source.txt").read_text() == "DUDUDU\n你好"
    assert (archive / "audio.txt").read_text() == "嘟嘟嘟\n你好"
    trace = json.loads((archive / "pronunciation_trace.json").read_text())
    assert trace["substitutions"][0]["display"] == "DUDUDU"
    assert [line["text"] for line in parse_lrc(rendered[0])] == ["DUDUDU", "你好"]
    assert (tmp_path / "output.mp4").read_bytes() == b"rendered"
