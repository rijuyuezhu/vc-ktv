"""No model downloads/GPU necessary to test the upstream media-to-MP4 adapter."""

import json
import subprocess
from pathlib import Path

import pytest

from ktv_app.config import Settings
from ktv_app.timeline import Timeline, Unit
from vc_ktv import upstream_pipeline as pipeline


def fake_timeline(
    audio, lines, duration, origin, work, settings, event, max_input_seconds
):
    assert Path(audio).is_file()
    assert max_input_seconds == 90
    assert len(lines) == 2
    assert [l.text for l in lines] == ["DUDUDU", "你好"]
    lines[0].units = [
        Unit(
            id="scat",
            text=lines[0].text,
            source_start=0,
            source_end=len(lines[0].text),
            start=0.2,
            end=1.0,
        )
    ]
    lines[1].units = [
        Unit(
            id="hello",
            text="你好",
            source_start=0,
            source_end=2,
            start=1.2,
            end=1.8,
        )
    ]
    return Timeline(duration=duration, origin=origin, lines=lines)


@pytest.fixture
def media(tmp_path):
    video = tmp_path / "original.mp4"
    ffmpeg = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240:r=25:d=2.2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=500:sample_rate=48000:duration=2.2",
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        "-shortest",
        str(video),
    ]
    subprocess.run(ffmpeg, check=True, capture_output=True)
    return video


def test_full_adapter_calls_upstream_engine_and_preserves_audio(
    media, tmp_path, monkeypatch
):
    def separation(video, original, info, folder, method, settings, event):
        assert method == "kim"
        assert Path(original).is_file()
        return original, {"method": "mock_kim"}

    monkeypatch.setattr(pipeline, "prepare_alignment_audio", separation)
    monkeypatch.setattr(pipeline, "align_reliable_blocks", fake_timeline)
    work = tmp_path / "attempt01"
    progress = []
    result = pipeline.make_karaoke_video(
        media, "DUDUDU\n你好", work, Settings(), progress=progress.append
    )
    assert result.video.exists() and result.ass.exists()
    assert "DUDUDU" in result.ass.read_text(encoding="utf-8-sig")
    assert "你好" in result.ass.read_text(encoding="utf-8-sig")
    assert len(progress) >= 5
    timeline = json.loads(result.timeline.read_text())
    assert [l["text"] for l in timeline["lines"]] == ["DUDUDU", "你好"]
    assert timeline["lines"][0]["units"][0]["timing_source"] == "model"
    assert json.loads(result.run_metadata.read_text())["status"] == "done"

    # Render from original video, not from Kim vocal separation.
    def audio_packets(path):
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-f",
            "md5",
            "-",
        ]
        return subprocess.check_output(cmd)

    assert audio_packets(media) == audio_packets(result.video)
    with pytest.raises(FileExistsError):
        pipeline.make_karaoke_video(media, "你好", work, Settings())


def test_bad_input_and_safe_failure(tmp_path, monkeypatch):
    missing = tmp_path / "nonexistent.mp4"
    with pytest.raises(FileNotFoundError):
        pipeline.make_karaoke_video(missing, "你好", tmp_path / "out", Settings())
    source = tmp_path / "video.mp4"
    source.write_bytes(b"test")
    with pytest.raises(ValueError, match="歌词"):
        pipeline.make_karaoke_video(source, "", tmp_path / "out", Settings())
    with pytest.raises(ValueError, match="制作目录"):
        pipeline.make_karaoke_video(source, "你好", tmp_path, Settings())

    def fail(source, workdir, settings):
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr(pipeline, "extract", fail)
    output = tmp_path / "failed-run"
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        pipeline.make_karaoke_video(source, "你好", output, Settings())
    report = json.loads((output / "run.json").read_text())
    assert report["status"] == "failed" and report["stage"] == "extract"
    assert "ffmpeg failed" in report["error"]
    assert not (output / "subtitles" / "karaoke.mp4").exists()


def test_estimated_words_are_marked_in_rendered_bundle(media, tmp_path, monkeypatch):
    def separation(video, original, info, folder, method, settings, event):
        return original, {"method": "mock"}

    def partial(
        audio, lines, duration, origin, work, settings, event, max_input_seconds
    ):
        lines[0].units = [
            Unit(
                id="u0",
                text="DUDUDU",
                source_start=0,
                source_end=6,
                start=0.2,
                end=0.9,
            )
        ]
        # Second line has no model timing: downstream may estimate it but must label it.
        return Timeline(duration=duration, lines=lines)

    monkeypatch.setattr(pipeline, "prepare_alignment_audio", separation)
    monkeypatch.setattr(pipeline, "align_reliable_blocks", partial)
    result = pipeline.make_karaoke_video(
        media, "DUDUDU\n你好", tmp_path / "estimated", Settings()
    )
    timeline = json.loads(result.timeline.read_text())
    assert timeline["lines"][1]["text"] == "你好"
    assert any(
        unit["timing_source"] == "estimated" for unit in timeline["lines"][1]["units"]
    )
    assert json.loads(result.report.read_text())["estimated_lines"] >= 1
