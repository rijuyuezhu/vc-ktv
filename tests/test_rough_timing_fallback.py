"""Automatic karaoke must output lyrics even if Kim, phonemization and TIFA fail."""

import json
import subprocess

import numpy as np
import soundfile as sf

from ktv_app.config import Settings
from ktv_app.timeline import Line, Timeline, Unit
from vc_ktv.global_alignment import pronunciation_lyrics
from vc_ktv.rough_timing import approximate_timeline, vocal_range


def test_unknown_scat_maps_to_approximate_pronunciation_preserving_display():
    original = "BIBIBI BAPABAPA\n我来啦 BZBZBZ woohoooo\nDUDUDU DaLaDa"
    spoken, mapping, changes = pronunciation_lyrics(original, unknown_vocables=True)
    assert len(spoken.splitlines()) == 3
    assert spoken.splitlines()[0] == "啦啦啦 啦啦啦啦"
    assert len(spoken.splitlines()[1]) < len(original.splitlines()[1])
    assert spoken.splitlines()[2] == "嘟嘟嘟 哒啦哒"
    assert any(x["display"] == "BAPABAPA" for x in changes)
    assert mapping[0][0] == (0, 6)
    assert all(a < b for spans in mapping for a, b in spans)
    assert original.splitlines()[0] == "BIBIBI BAPABAPA"


def test_last_resort_rough_timeline_preserves_complete_text_and_order(tmp_path):
    rate, duration = 16000, 12.0
    waveform = np.zeros(int(rate * duration), dtype="float32")
    waveform[rate * 2 : rate * 9] = 0.1
    wav = tmp_path / "vocals.wav"
    sf.write(wav, waveform, rate)
    left, right = vocal_range(wav, duration)
    assert 1.9 < left < 2.1
    assert 8.9 < right < 9.2
    lyrics = "太阳是什么颜色\nBIBIBI BAPABAPA\n日出日落\nDaLaDa"
    t = approximate_timeline(lyrics, duration, audio=wav, reason="OOV")
    assert [x.text for x in t.lines] == lyrics.splitlines()
    assert t.diagnostics["estimated_lines"] == 4
    assert t.diagnostics["failure"] == "OOV"
    assert all(line.start < line.end for line in t.lines)
    assert all(a.end <= b.start for a, b in zip(t.lines, t.lines[1:]))
    assert all(u.timing_source == "estimated" for line in t.lines for u in line.units)
    assert approximate_timeline(lyrics, duration, audio=tmp_path / "missing.wav").lines


def _small_video(path):
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=25:d=3.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=500:duration=3.2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_unknown_vocable_retry_produces_real_video_with_original_display(
    tmp_path, monkeypatch
):
    from vc_ktv import global_alignment, upstream_pipeline

    video = tmp_path / "source.mp4"
    _small_video(video)
    calls = []

    def fake_full(
        source, lyrics, duration, origin, work, settings, *, unknown_vocables=False
    ):
        calls.append(unknown_vocables)
        if not unknown_vocables:
            raise RuntimeError("TIFA OOV: BIBIBI")
        spoken, _, changes = pronunciation_lyrics(lyrics, unknown_vocables=True)
        assert "BIBIBI" not in spoken and changes
        return Timeline(
            duration=duration,
            lines=[
                Line(
                    id="line",
                    text=lyrics,
                    start=0.5,
                    end=2.6,
                    units=[
                        Unit(
                            id="unit",
                            text=lyrics,
                            source_start=0,
                            source_end=len(lyrics),
                            start=0.5,
                            end=2.6,
                        )
                    ],
                )
            ],
        )

    monkeypatch.setattr(global_alignment, "align_full_song", fake_full)
    result = upstream_pipeline.make_karaoke_video(
        video, "BIBIBI BAPABAPA", tmp_path / "retry", Settings(), mode="global"
    )
    assert calls == [False, True]
    timeline = json.loads(result.timeline.read_text())
    assert timeline["lines"][0]["text"] == "BIBIBI BAPABAPA"
    assert timeline["diagnostics"]["fallback_attempts"][0]["stage"] == "tifa"
    assert result.video.stat().st_size > 0


def test_no_models_at_all_still_makes_karaoke_mp4(tmp_path, monkeypatch):
    from vc_ktv import global_alignment, upstream_pipeline

    video = tmp_path / "source.mp4"
    _small_video(video)
    seen = []

    def broken_kim(*args, **kwargs):
        seen.append("kim_failed")
        raise RuntimeError("Kim unavailable")

    def broken_tifa(*args, **kwargs):
        seen.append("tifa_failed")
        raise RuntimeError("all models unavailable")

    monkeypatch.setattr(upstream_pipeline, "prepare_alignment_audio", broken_kim)
    monkeypatch.setattr(global_alignment, "align_full_song", broken_tifa)
    result = upstream_pipeline.make_karaoke_video(
        video,
        "BIBIBI\n跟着节奏\nLaLaLa",
        tmp_path / "rough",
        Settings(),
        mode="global-kim",
    )
    assert seen == ["kim_failed", "tifa_failed", "tifa_failed"]
    report = json.loads(result.report.read_text())
    timeline = json.loads(result.timeline.read_text())
    assert result.video.stat().st_size > 0
    assert report["rendered_lines"] == 3
    assert report["estimated_lines"] == 3
    assert report["alignment_quality"] == "rough"
    assert len(report["fallback_attempts"]) == 3
    assert report["unmatched_lines"] == 0
    assert timeline["diagnostics"]["pipeline"] == "last_resort_rough_timing"
    assert [r["stage"] for r in timeline["diagnostics"]["fallback_attempts"]] == [
        "kim",
        "tifa",
        "tifa-vocables",
    ]
    assert [line["text"] for line in timeline["lines"]] == [
        "BIBIBI",
        "跟着节奏",
        "LaLaLa",
    ]
