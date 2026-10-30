"""Best-effort automatic TIFA text recovery, without relaxing manual imports."""

import json
import subprocess
from pathlib import Path

import pytest
from textgrid import IntervalTier, TextGrid

from ktv_app.config import Settings
from ktv_app.subtitles import write_bundle
from ktv_app.timeline import Style
from vc_ktv.auto_textgrid_repair import reconcile_lyrics
from vc_ktv.global_alignment import align_full_song
from vc_ktv.karaoke import parse_lrc
from vc_ktv.textgrid_import import convert_textgrid


def model_grid(path: Path, tokens):
    grid = TextGrid(minTime=0, maxTime=60)
    tier = IntervalTier(name="texts", minTime=0, maxTime=60)
    for start, end, text in tokens:
        tier.add(start, end, text)
    grid.append(tier)
    grid.write(str(path))
    return path


@pytest.mark.parametrize(
    "model_words,missing",
    [
        (["你", "好", "下", "一", "句"], False),
        (["你", "下", "一", "句"], True),
        (["你", "好", "下", "句"], True),
        (["好", "下", "一", "句"], True),
    ],
)
def test_repair_only_missing_words_preserves_model_intervals(
    tmp_path, model_words, missing
):
    times = [(0.5 + i * 0.35, 0.7 + i * 0.35, w) for i, w in enumerate(model_words)]
    source = model_grid(tmp_path / "raw.TextGrid", times)
    if missing:
        with pytest.raises(ValueError, match="对齐文本"):
            convert_textgrid(source, "你好\n下一句", 6)
    before = source.read_bytes()
    report = reconcile_lyrics(source, ["你好", "下一句"], tmp_path / "fixed.TextGrid")
    assert source.read_bytes() == before
    lrc, result = convert_textgrid(tmp_path / "fixed.TextGrid", "你好\n下一句", 6)
    assert [x["text"] for x in parse_lrc(lrc)] == ["你好", "下一句"]
    assert report["changed_intervals"] == (1 if missing else 0)
    if missing:
        assert report["diff"][0]["operation"] == "delete"
    assert result["lines"] == 2


def test_best_effort_on_actual_pipeline_marks_one_word_estimated(tmp_path, monkeypatch):
    from vc_ktv import global_alignment

    source = tmp_path / "in.mp4"
    source.write_bytes(b"no-media-needed")
    tifa = tmp_path / "tifa"
    tifa.mkdir()
    (tifa / "infer.py").touch()
    python = tmp_path / "python"
    python.touch()
    model = tmp_path / "model.pt"
    model.touch()
    settings = Settings(tifa_source=tifa, tifa_python=python, tifa_model=model)

    def fake_extract(command, **kwargs):
        (tmp_path / "attempt" / "audio.wav").write_bytes(b"model-mock")

    def fake_model(args, **kwargs):
        model_grid(
            tmp_path / "attempt" / "alignment" / "audio.TextGrid",
            [(0.4, 0.8, "你"), (0.8, 1.1, "好"), (1.1, 1.4, "下"), (1.4, 1.6, "句")],
        )
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(global_alignment, "run", fake_extract)
    monkeypatch.setattr(global_alignment.subprocess, "run", fake_model)
    timeline = align_full_song(
        source, "你好\n下一句", 6, 0, tmp_path / "attempt", settings
    )
    assert len(timeline.lines) == 2
    estimated = [
        unit
        for line in timeline.lines
        for unit in line.units
        if unit.timing_source == "estimated"
    ]
    assert len(estimated) == 1
    assert timeline.diagnostics["estimated_lines"] == 1
    assert "一" in estimated[0].text
    assert timeline.diagnostics["best_effort"]["diff"][0]["expected"] == "一"
    assert (tmp_path / "attempt" / "alignment" / "audio.TextGrid").exists()
    assert (tmp_path / "attempt" / "repaired.TextGrid").exists()
    write_bundle(
        timeline, Style(mode="single", auto_complete=True), tmp_path / "bundle"
    )
    report = json.loads((tmp_path / "bundle" / "export_report.json").read_text())
    assert report["estimated_units"] == 1
    assert report["alignment_quality"] == "repaired"
    assert report["rendered_lines"] == 2
    assert report["best_effort"]["changed_intervals"] == 1


def test_automatic_millisecond_stamp_preserves_tifa_1ms_intervals(tmp_path):
    from vc_ktv.global_alignment import timeline_from_lrc

    raw = model_grid(
        tmp_path / "subcentisecond.TextGrid",
        [(0.500, 0.502, "你"), (0.502, 0.504, "好"), (0.504, 0.510, "吗")],
    )
    lrc, _ = convert_textgrid(raw, "你好吗", 6, millisecond_timestamps=True)
    assert "00:00.502" in lrc
    timeline = timeline_from_lrc(lrc, "你好吗", 6)
    assert len(timeline.lines[0].units) == 3
    assert [round(u.start, 3) for u in timeline.lines[0].units] == [0.500, 0.502, 0.504]


def test_manual_strict_import_remains_strict(tmp_path):
    source = model_grid(tmp_path / "raw.TextGrid", [(0.5, 0.7, "你"), (0.8, 1.0, "吗")])
    with pytest.raises(ValueError, match="对齐文本"):
        convert_textgrid(source, "你好吗", 6)


@pytest.mark.parametrize("drop_index", [None, 0, 2, 7, 13, 20])
def test_repeated_scat_lyrics_roundtrip_best_effort(tmp_path, drop_index):
    """BV1yVQFBNEPn: DUDUDU, DUDUDUDU and DaLaDa must survive to output."""
    from vc_ktv.global_alignment import pronunciation_lyrics, timeline_from_lrc
    from vc_ktv.textgrid_import import chars

    lyrics = (
        "太阳该是什么颜色\n"
        "DUDUDU DUDUDUDU\n"
        "唱起这首歌 DaLaDa\n"
        "DUDUDU DUDUDUDU\n"
        "我们永远这样唱着"
    )
    spoken, spans, changes = pronunciation_lyrics(lyrics)
    assert len(changes) == 5
    assert spoken.splitlines()[1] == "嘟嘟嘟 嘟嘟嘟嘟"
    assert spoken.splitlines()[2] == "唱起这首歌 哒啦哒"
    expected = "".join(chars(line)[0] for line in spoken.splitlines())
    emitted = [c for i, c in enumerate(expected) if i != drop_index]
    times = [(0.2 + i * 0.3, 0.4 + i * 0.3, c) for i, c in enumerate(emitted)]
    raw = model_grid(tmp_path / "model.TextGrid", times)
    if drop_index is not None:
        reconcile_lyrics(raw, spoken.splitlines(), tmp_path / "repaired.TextGrid")
        raw = tmp_path / "repaired.TextGrid"
    lrc, report = convert_textgrid(
        raw, lyrics, 60, pronunciation=spoken, source_spans=spans
    )
    timeline = timeline_from_lrc(lrc, lyrics, 60)
    assert [line.text for line in timeline.lines] == lyrics.splitlines()
    assert timeline.lines[1].units[0].text == "DUDUDU"
    assert "DUDUDUDU" in "".join(u.text for u in timeline.lines[1].units)
    assert "DaLaDa" in "".join(u.text for u in timeline.lines[2].units)
    assert report["lines"] == 5
