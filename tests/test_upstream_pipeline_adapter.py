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
    assert max_input_seconds is None
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
        media,
        "DUDUDU\n你好",
        work,
        Settings(),
        progress=progress.append,
        mode="quality",
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
        pipeline.make_karaoke_video(media, "你好", work, Settings(), mode="quality")


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
    with pytest.raises(RuntimeError, match="moov atom|Invalid data"):
        pipeline.make_karaoke_video(source, "你好", output, Settings())
    report = json.loads((output / "run.json").read_text())
    assert report["status"] == "failed" and report["stage"] == "extract"
    assert "moov atom" in report["error"] or "Invalid data" in report["error"]
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
        media, "DUDUDU\n你好", tmp_path / "estimated", Settings(), mode="quality"
    )
    timeline = json.loads(result.timeline.read_text())
    assert timeline["lines"][1]["text"] == "你好"
    assert any(
        unit["timing_source"] == "estimated" for unit in timeline["lines"][1]["units"]
    )
    assert json.loads(result.report.read_text())["estimated_lines"] >= 1


@pytest.mark.parametrize(
    ("failure", "expected_stages"),
    [
        ("extract", ["extract"]),
        ("preprocess", ["preprocess"]),
        ("kim", ["kim"]),
        ("asr", ["asr-tifa"]),
        ("completion", ["completion"]),
        ("subtitles", ["subtitles"]),
        ("render", ["render"]),
    ],
)
def test_quality_best_effort_keeps_all_lyrics_and_renders_real_mp4(
    media, tmp_path, monkeypatch, failure, expected_stages
):
    """All recoverable failures still render full visible lyrics with original audio."""
    import vc_ktv.upstream_pipeline as upstream

    lyrics = "扬州的风轻轻吹过\n你看见满天星星\n我们还可以再见\nDUDUDU DaLaDa"
    original_extract = upstream.extract
    original_preprocess = upstream.preprocess
    original_complete = upstream.complete_timeline
    original_write = upstream.write_bundle
    original_render = upstream.render
    calls = {"render": 0, "align": 0, "write": 0}

    def broken_extract(*args, **kwargs):
        if failure == "extract":
            raise RuntimeError("extractor temporarily unavailable")
        return original_extract(*args, **kwargs)

    def broken_preprocess(*args, **kwargs):
        if failure == "preprocess":
            raise ValueError("invalid lyric preprocessor output")
        return original_preprocess(*args, **kwargs)

    def kim(video, original, info, folder, method, settings, event):
        if failure == "kim":
            raise RuntimeError("Kim GPU allocation failed")
        return original, {"source": "mock"}

    def align(audio, lines, duration, origin, work, settings, event, max_input_seconds):
        calls["align"] += 1
        if failure == "asr":
            raise ValueError(
                "识别结果与已确认歌词之间没有足够的可信切点，无法生成小于90秒的对齐片段"
            )
        if failure == "kim":
            assert audio.name == "audio.wav"  # Original mix retry.
        return Timeline(duration=duration, origin=origin, lines=lines)

    def completion(*args, **kwargs):
        if failure == "completion":
            raise ValueError("completion has inconsistent markers")
        return original_complete(*args, **kwargs)

    def write(*args, **kwargs):
        calls["write"] += 1
        if failure == "subtitles" and calls["write"] == 1:
            raise ValueError("subtitle bundle invalid")
        return original_write(*args, **kwargs)

    def render(*args, **kwargs):
        calls["render"] += 1
        if failure == "render" and calls["render"] == 1:
            raise RuntimeError("ffmpeg encoder ran out of resources")
        return original_render(*args, **kwargs)

    monkeypatch.setattr(upstream, "extract", broken_extract)
    monkeypatch.setattr(upstream, "preprocess", broken_preprocess)
    monkeypatch.setattr(upstream, "prepare_alignment_audio", kim)
    monkeypatch.setattr(upstream, "align_reliable_blocks", align)
    monkeypatch.setattr(upstream, "complete_timeline", completion)
    monkeypatch.setattr(upstream, "write_bundle", write)
    monkeypatch.setattr(upstream, "render", render)
    result = upstream.make_karaoke_video(
        media, lyrics, tmp_path / ("quality-" + failure), Settings(), mode="quality"
    )
    report = json.loads(result.report.read_text())
    timeline = json.loads(result.timeline.read_text())
    assert result.video.stat().st_size > 1024
    assert report["rendered_lines"] == 4
    assert [l["text"] for l in timeline["lines"]] == lyrics.splitlines()
    assert [f["stage"] for f in report["fallback_attempts"]] == expected_stages
    if failure in ("extract", "preprocess", "asr", "completion", "subtitles"):
        assert report["alignment_quality"] == "rough"
        assert report["estimated_lines"] == 4
        assert all(
            u["timing_source"] == "estimated"
            for l in timeline["lines"]
            for u in l["units"]
        )
        assert "DUDUDU DaLaDa" in result.ass.read_text(encoding="utf-8-sig")
    elif failure == "kim":
        assert report["alignment_quality"] == "model"
        assert calls["align"] == 1
    else:
        assert calls["render"] == 2
    assert json.loads(result.run_metadata.read_text())["status"] == "done"

    # Audio stream remains copied from the original media.
    def checksum(path):
        return subprocess.check_output(
            [
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
        )

    assert checksum(media) == checksum(result.video)


def test_quality_rough_failure_still_records_hard_render_error(
    media, tmp_path, monkeypatch
):
    def broken_align(*args, **kwargs):
        raise ValueError("not enough credible cuts")

    def broken_render(*args, **kwargs):
        raise RuntimeError("libass is unavailable")

    monkeypatch.setattr(
        pipeline,
        "prepare_alignment_audio",
        lambda source, original, info, folder, method, settings, event: (
            original,
            {"source": "mock"},
        ),
    )
    monkeypatch.setattr(pipeline, "align_reliable_blocks", broken_align)
    monkeypatch.setattr(pipeline, "render", broken_render)
    out = tmp_path / "unrenderable"
    with pytest.raises(RuntimeError, match="libass is unavailable"):
        pipeline.make_karaoke_video(
            media, "第一句歌词\n第二句歌词", out, Settings(), mode="quality"
        )
    assert json.loads((out / "run.json").read_text())["status"] == "failed"
    assert (out / "subtitles" / "lyrics.ass").is_file()
