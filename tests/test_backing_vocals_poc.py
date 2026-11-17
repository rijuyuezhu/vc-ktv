"""Synthetic audio checks for the optional offline backing-vocals benchmark."""

from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
import soundfile as sf

from benchmarks import backing_vocals as poc


def wave(path, samples=4096, amplitude=0.3, rate=poc.SAMPLE_RATE, channels=2):
    signal = np.full((samples, channels), amplitude, dtype=np.float32)
    sf.write(path, signal, rate, subtype="FLOAT")
    return path


def test_mix_preserves_instruments_and_harmonies(tmp_path):
    length = poc.BLOCK * 2 + 23
    inst = wave(tmp_path / "inst.wav", length, 0.3)
    harmony = wave(tmp_path / "harmony.wav", length - 15, 0.2)
    target = tmp_path / "mixed.wav"

    gain = poc.combine_instruments_and_harmonies(inst, harmony, target)

    combined, sr = sf.read(target, dtype="float32", always_2d=True)
    assert gain == 1
    assert sr == poc.SAMPLE_RATE
    assert len(combined) == length
    np.testing.assert_allclose(combined[: length - 15], 0.5, atol=3e-7)
    np.testing.assert_allclose(combined[-15:], 0.3, atol=3e-7)


def test_mix_prevents_fixed_point_clipping(tmp_path):
    inst = wave(tmp_path / "inst.wav", 350, 0.8)
    harmony = wave(tmp_path / "harmony.wav", 350, 0.8)
    target = tmp_path / "mixed.wav"

    gain = poc.combine_instruments_and_harmonies(inst, harmony, target)

    samples, _ = sf.read(target)
    assert gain == pytest.approx(0.99 / 1.6, abs=1e-7)
    assert float(np.max(np.abs(samples))) == pytest.approx(0.99, abs=1e-6)


@pytest.mark.parametrize(
    "samples,rate,channels",
    [
        (300, 48000, 2),
        (300, 44100, 1),
        (0, 44100, 2),
    ],
)
def test_rejects_invalid_audio(tmp_path, samples, rate, channels):
    path = wave(tmp_path / "invalid.wav", samples, rate=rate, channels=channels)
    with pytest.raises((ValueError, RuntimeError)):
        poc.validate_audio(path)


def test_rejects_stems_with_different_length(tmp_path):
    inst = wave(tmp_path / "inst.wav", 1000)
    harmony = wave(tmp_path / "harmony.wav", 1000 + poc.SAMPLE_RATE // 5)
    with pytest.raises(ValueError, match="100 ms"):
        poc.combine_instruments_and_harmonies(inst, harmony, tmp_path / "out.wav")


class FakeSeparator:
    loaded_models: ClassVar[list[str]] = []

    def __init__(self, **settings):
        self.directory = Path(settings["output_dir"])

    def load_model(self, *, model_filename):
        self.model = model_filename
        self.loaded_models.append(model_filename)

    def separate(self, source, names):
        original, rate = sf.read(source, dtype="float32", always_2d=True)
        vocals, other = [self.directory / (name + ".wav") for name in names.values()]
        # First pass: all vocals + instruments; second pass: lead + harmonies.
        v_ratio = 0.35 if self.model == poc.VOCALS_MODEL else 0.8
        sf.write(vocals, original * v_ratio, rate, subtype="FLOAT")
        sf.write(other, original * (1 - v_ratio), rate, subtype="FLOAT")
        return [str(vocals), str(other)]


@pytest.mark.parametrize(
    "mode,models",
    [
        ("direct", [poc.KARAOKE_MODEL]),
        ("two-stage", [poc.VOCALS_MODEL, poc.KARAOKE_MODEL]),
    ],
)
def test_completes_audio_poc_without_real_models(tmp_path, mode, models):
    FakeSeparator.loaded_models: ClassVar[list[str]] = []
    source = wave(tmp_path / "input.wav", 8820, 0.3)
    output = tmp_path / mode
    report = poc.run(
        source, output, mode=mode, model_dir=tmp_path, backend=FakeSeparator
    )
    assert FakeSeparator.loaded_models == models
    assert report["mode"] == mode
    assert report["models"] == models
    assert report["backing_duration_delta_ms"] == 0
    assert report["target_to_original_rms"] > 0.2
    assert report["candidate_rms"] > 0
    assert report["encoded_bytes"] > 0
    assert (output / "backing.m4a").is_file()
    assert (output / "report.json").is_file()
    assert "input" not in (output / "report.json").read_text()
    data, _ = sf.read(
        output / ("backing.wav" if mode == "two-stage" else "lead-vocals/other.wav")
    )
    expected = 0.72 * 0.3 if mode == "two-stage" else 0.2 * 0.3
    np.testing.assert_allclose(data, expected, atol=3e-6)


def test_rejects_incorrect_model_output_labels(tmp_path):
    class WrongStems(FakeSeparator):
        def separate(self, source, names):
            return ["surprise.wav", "different.wav"]

    source = wave(tmp_path / "source.wav")
    with pytest.raises(RuntimeError, match="Unexpected separation stems"):
        poc.separate(
            source,
            tmp_path / "stage",
            poc.KARAOKE_MODEL,
            model_dir=tmp_path,
            backend=WrongStems,
        )


def test_rejects_unknown_route(tmp_path):
    with pytest.raises(ValueError, match="Unknown separation route"):
        poc.run(tmp_path / "missing.wav", tmp_path, mode="wrong", model_dir=tmp_path)


@pytest.mark.parametrize("role", ["vocals", "karaoke"])
def test_cuda_worker_request_without_gpu(monkeypatch, tmp_path, role):
    """Verify the real-worker protocol without loading checkpoints in CI."""
    import json
    import sys

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "model.ckpt").write_bytes(b"fake-weights")
    model_source = tmp_path / "model-source"
    model_source.mkdir()
    mixture = wave(tmp_path / "mixture.wav", amplitude=0.27)
    invoked = []

    def fake_run(command, *, check):
        assert check
        assert command[0] == sys.executable
        assert Path(command[1]).name == "backing_vocals_worker.py"
        req = json.loads(Path(command[2]).read_text())
        invoked.append(req)
        assert req["checkpoint"] == str(model_dir / "model.ckpt")
        assert req["model_source"] == str(model_source)
        assert req["role"] == role
        assert req["mixture"] == str(mixture)
        for name, level in (("vocals.wav", 0.2), ("other.wav", 0.07)):
            wave(Path(req["output_dir"]) / name, amplitude=level)

    monkeypatch.setattr(poc.subprocess, "run", fake_run)
    target, other, seconds = poc.separate(
        mixture,
        tmp_path / "stage",
        "model.ckpt",
        model_dir=model_dir,
        model_source=model_source,
        worker_python=Path(sys.executable),
        role=role,
    )
    assert (target.name, other.name) == ("vocals.wav", "other.wav")
    assert len(invoked) == 1
    assert seconds >= 0


def test_requires_real_model_source_and_checkpoint(tmp_path):
    mixture = wave(tmp_path / "mix.wav")
    with pytest.raises(ValueError, match="model-source"):
        poc.separate(mixture, tmp_path / "stage", "model.ckpt", model_dir=tmp_path)
    model_source = tmp_path / "source"
    model_source.mkdir()
    with pytest.raises(FileNotFoundError, match="model.ckpt"):
        poc.separate(
            mixture,
            tmp_path / "stage",
            "model.ckpt",
            model_dir=tmp_path,
            model_source=model_source,
        )
