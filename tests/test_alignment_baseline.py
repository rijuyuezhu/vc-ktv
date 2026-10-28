import subprocess
from argparse import Namespace
from pathlib import Path

import pytest
from textgrid import IntervalTier, TextGrid

from scripts import alignment_baseline


def inputs(tmp_path):
    folder = tmp_path / "tools"
    folder.mkdir()
    (folder / "infer.py").write_text("fixture")
    model = folder / "model.pt"
    model.write_bytes(b"fixture-model")
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"fixture-interpreter")
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"fixture-wav")
    lyrics = tmp_path / "song.txt"
    lyrics.write_text("DUDUDU\n你好")
    args = Namespace(
        audio=audio,
        lyrics=lyrics,
        out=tmp_path / "output",
        tifa=folder,
        model=model,
        python=interpreter,
        confirm_rights=False,
        mode="pronunciation",
        language="zh",
        timeout=20,
    )
    return args


def test_baseline_requires_confirmation_and_fresh_output(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    with pytest.raises(ValueError, match="authorized"):
        alignment_baseline.run(args)
    assert not args.out.exists()
    args.confirm_rights = True

    def fake(command, **kwargs):
        if command[0] != "git":
            assert Path(command[2]).name == "audio.wav"
            assert Path(command[2]).with_suffix(".txt").is_file()
            kwargs["stdout"].write("Skipping: Unknown phonemes\n")
        return subprocess.CompletedProcess(
            command, 0, stdout="test-commit" if command[0] == "git" else ""
        )

    monkeypatch.setattr(alignment_baseline.subprocess, "run", fake)
    result = alignment_baseline.run(args)
    assert result["error"] == "alignment_oov"
    assert result["returncode"] == 0
    assert result["textgrid_exists"] is False
    assert (args.out / "audio.txt").read_text() == "嘟嘟嘟\n你好"
    assert (args.out / "lyrics.original.txt").read_text() == "DUDUDU\n你好"
    assert (args.out / "result.json").exists()
    with pytest.raises(FileExistsError):
        alignment_baseline.run(args)


def test_baseline_converts_tifa_native_output_without_fake_accuracy(
    tmp_path, monkeypatch
):
    args = inputs(tmp_path)
    args.confirm_rights = True

    def fake(command, **kwargs):
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout="test-commit")
        output = Path(command[command.index("-o") + 1])
        grid = TextGrid(maxTime=3.0)
        tier = IntervalTier(name="texts", minTime=0.0, maxTime=3.0)
        for start, end, word in (
            (0.1, 0.2, "嘟"),
            (0.2, 0.3, "嘟"),
            (0.3, 0.4, "嘟"),
            (0.6, 0.8, "你"),
            (0.8, 1.0, "好"),
        ):
            tier.add(start, end, word)
        grid.append(tier)
        grid.write(str(output / "audio.TextGrid"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(alignment_baseline.subprocess, "run", fake)
    result = alignment_baseline.run(args)
    assert result["error"] is None
    assert result["alignment_report"]["semantic_units"] == 5
    assert result["manually_labelled_accuracy"] is False
    assert result["gpu_memory_peak_mib"] is None
    lrc = (args.out / "aligned.lrc").read_text()
    assert "DUDUDU" in lrc and "嘟嘟嘟" not in lrc
    from vc_ktv.karaoke import parse_lrc

    assert [line["text"] for line in parse_lrc(lrc)] == ["DUDUDU", "你好"]
    assert (args.out / "audio.wav").read_bytes() == args.audio.read_bytes()
