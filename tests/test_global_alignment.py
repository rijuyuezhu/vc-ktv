"""Restore strict whole-song alignment: no fabricated rolling timestamps."""

import json
import subprocess

import pytest

from ktv_app.config import Settings
from tests.test_textgrid_import import grid_file
from vc_ktv.global_alignment import (
    align_full_song,
    pronunciation_lyrics,
    timeline_from_lrc,
)
from vc_ktv.textgrid_import import convert_textgrid


def test_recovered_old_scat_mapping_preserves_original_display():
    spoken, spans, changes = pronunciation_lyrics("DUDUDU\n你好吗")
    assert spoken.splitlines() == ["嘟嘟嘟", "你好吗"]
    assert spans[0] == [(0, 6)] * 3
    assert changes[0]["display"] == "DUDUDU"


def test_full_song_strict_textgrid_to_timeline_with_punctuation(tmp_path):
    grid = grid_file(tmp_path / "alignment.TextGrid")
    lyrics = "你，好!\n下一句"
    lrc, _ = convert_textgrid(grid, lyrics, 4)
    t = timeline_from_lrc(lrc, lyrics, 4)
    assert [line.text for line in t.lines] == ["你，好!", "下一句"]
    assert [u.text for u in t.lines[0].units] == ["你", "，好!"]
    assert t.lines[0].units[0].start == 0.5
    assert t.lines[-1].units[-1].end == 2.55
    assert all(u.timing_source == "model" for l in t.lines for u in l.units)
    assert t.diagnostics["pipeline"] == "global_full_song_tifa"
    assert t.diagnostics["estimated_lines"] == 0


def test_full_song_preserves_strict_words_but_reports_suspicious_timing(tmp_path):
    grid = grid_file(tmp_path / "alignment.TextGrid")
    lrc, _ = convert_textgrid(grid, "你，好!\n下一句", 4)
    with pytest.raises(ValueError, match="顺序"):
        timeline_from_lrc(lrc, "你，好!\n下两句", 4)
    bad = "\n".join(
        f"[00:01.{i * 10:02}]<00:01.{i * 10:02}>你好<00:01.{i * 10 + 5:02}>"
        for i in range(4)
    )
    collapsed = timeline_from_lrc(bad, "你好\n你好\n你好\n你好", 4)
    assert collapsed.diagnostics["suspicious_timing"]


def test_global_mode_executes_one_tifa_process_and_saves_raw_evidence(
    tmp_path, monkeypatch
):
    from vc_ktv import global_alignment

    tifa = tmp_path / "tifa"
    tifa.mkdir()
    (tifa / "infer.py").touch()
    python = tmp_path / "python"
    python.touch()
    model = tmp_path / "model.pt"
    model.touch()
    source = tmp_path / "video.mp4"
    source.touch()
    settings = Settings(tifa_source=tifa, tifa_python=python, tifa_model=model)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        (tmp_path / "run" / "audio.wav").write_bytes(b"WAV")

    def fake_infer(args, **kwargs):
        assert args[0] == str(python)
        assert args[-6:] == [
            "--batch-size",
            "1",
            "--num-workers",
            "0",
            "--skip-handling",
            "preserve",
        ]
        assert kwargs["cwd"] == tifa
        grid_file(tmp_path / "run" / "alignment" / "audio.TextGrid")
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(global_alignment, "run", fake_run)
    monkeypatch.setattr(global_alignment.subprocess, "run", fake_infer)
    result = align_full_song(
        source, "你，好!\n下一句", 4, 0, tmp_path / "run", settings
    )
    assert len(calls) == 2
    assert result.lines[1].text == "下一句"
    assert (tmp_path / "run" / "alignment" / "audio.TextGrid").exists()
    assert (tmp_path / "run" / "aligned.lrc").exists()
    assert (
        json.loads((tmp_path / "run" / "alignment_report.json").read_text())[
            "semantic_units"
        ]
        == 5
    )


def test_kim_full_song_adapter_keeps_native_times_and_original_audio(
    tmp_path, monkeypatch
):
    """The default trial must not invoke ASR, split songs, or estimate timing."""
    from ktv_app.timeline import Line, Timeline, Unit
    from vc_ktv import global_alignment, upstream_pipeline

    # Make an actual 2s AAC video and perform the real final MP4 render.
    video = tmp_path / "original.mp4"
    subprocess.run(
        [
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
        ],
        check=True,
        capture_output=True,
    )
    seen = []

    def fake_separation(source, original_audio, info, work, method, settings, event):
        assert method == "kim" and original_audio.is_file()
        seen.append("kim")
        return original_audio, {"method": "kim"}

    def fake_full(source, lyrics, duration, origin, work, settings):
        seen.append("full_tifa")
        assert lyrics == "你好世界"
        assert source.name == "audio.wav"
        return Timeline(
            duration=duration,
            origin=origin,
            lines=[
                Line(
                    id="line",
                    text="你好世界",
                    start=0.2,
                    end=1.6,
                    units=[
                        Unit(
                            id="u",
                            text="你好世界",
                            source_start=0,
                            source_end=4,
                            start=0.2,
                            end=1.6,
                        )
                    ],
                )
            ],
        )

    monkeypatch.setattr(upstream_pipeline, "prepare_alignment_audio", fake_separation)
    monkeypatch.setattr(global_alignment, "align_full_song", fake_full)
    result = upstream_pipeline.make_karaoke_video(
        video, "你好世界", tmp_path / "kim-global", Settings()
    )
    assert seen == ["kim", "full_tifa"]
    timeline = json.loads(result.timeline.read_text())
    assert timeline["lines"][0]["units"][0]["timing_source"] == "model"
    assert timeline["diagnostics"]["alignment_audio"]["method"] == "kim"
    report = json.loads(result.report.read_text())
    assert report["estimated_units"] == 0 and report["auto_complete"] is False
    assert result.video.is_file() and result.video.stat().st_size > 0

    def audio_packets(path):
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

    assert audio_packets(video) == audio_packets(result.video)
