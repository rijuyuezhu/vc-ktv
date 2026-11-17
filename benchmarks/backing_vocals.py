"""Offline lead-vocal removal POC; never used by the live preparation pipeline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import soundfile as sf

KARAOKE_MODEL = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
VOCALS_MODEL = "MelBandRoformer.ckpt"
SAMPLE_RATE = 44100
CHANNELS = 2
BLOCK = 65536


def ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", *args],
        check=True,
    )


def extract_audio(source: Path, destination: Path) -> None:
    ffmpeg(
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_f32le",
        str(destination),
    )
    validate_audio(destination)


def validate_audio(path: Path) -> sf._SoundFileInfo:
    info = sf.info(path)
    if info.samplerate != SAMPLE_RATE or info.channels != CHANNELS or info.frames <= 0:
        raise ValueError(
            f"Expected nonempty {SAMPLE_RATE} Hz stereo audio: {path.name}"
        )
    return info


def separate(
    source: Path,
    stage: Path,
    model: str,
    *,
    model_dir: Path,
    model_source: Path | None = None,
    worker_python: Path | None = None,
    role: str = "karaoke",
    backend=None,
) -> tuple[Path, Path, float]:
    """Produce target and residual stems using the existing Kim engine.

    The optional backend argument is only a seam for synthetic test models.
    Actual inference runs in the separate CUDA runtime, never in the server.
    """
    stage.mkdir(parents=True, exist_ok=True)
    expected = (stage / "vocals.wav", stage / "other.wav")
    started = time.monotonic()
    if backend is None:
        if model_source is None or not model_source.is_dir():
            raise ValueError("A valid --model-source is required for real separation")
        checkpoint = model_dir / model
        if not checkpoint.is_file():
            raise FileNotFoundError(f"Missing model checkpoint: {checkpoint.name}")
        request = stage / "request.json"
        request.write_text(
            json.dumps(
                {
                    "model_source": str(model_source),
                    "checkpoint": str(checkpoint),
                    "mixture": str(source),
                    "output_dir": str(stage),
                    "role": role,
                }
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(worker_python or Path(sys.executable)),
                str(Path(__file__).with_name("backing_vocals_worker.py")),
                str(request),
            ],
            check=True,
        )
    else:
        separator = backend(model_file_dir=str(model_dir), output_dir=str(stage))
        separator.load_model(model_filename=model)
        outputs = separator.separate(
            str(source), {"Vocals": "vocals", "Instrumental": "other"}
        )
        reported = {Path(name).name for name in outputs}
        if reported != {path.name for path in expected}:
            raise RuntimeError(f"Unexpected separation stems: {sorted(reported)}")
    for path in expected:
        validate_audio(path)
    return *expected, time.monotonic() - started


def _read(reader: sf.SoundFile, length: int) -> np.ndarray:
    data = reader.read(length, dtype="float32", always_2d=True)
    if len(data) == length:
        return data
    return np.pad(data, ((0, length - len(data)), (0, 0)))


def mix_stems(stems: tuple[Path, ...], destination: Path) -> float:
    """Combine one or more stems at their original relative gain, limiting peaks."""
    if not stems:
        raise ValueError("At least one stem is required")
    infos = [validate_audio(path) for path in stems]
    frames = infos[0].frames
    if any(abs(info.frames - frames) > SAMPLE_RATE // 10 for info in infos[1:]):
        raise ValueError("Stem length differs by more than 100 ms")

    def blocks():
        with ExitStack() as stack:
            readers = [stack.enter_context(sf.SoundFile(path)) for path in stems]
            for start in range(0, frames, BLOCK):
                length = min(BLOCK, frames - start)
                mixed = sum(_read(reader, length) for reader in readers)
                if not np.isfinite(mixed).all():
                    raise ValueError("Stem contains invalid samples")
                yield mixed

    peak = max((float(np.max(np.abs(block))) for block in blocks()), default=0.0)
    gain = min(1.0, 0.99 / peak) if peak else 1.0

    with sf.SoundFile(
        destination, "w", samplerate=SAMPLE_RATE, channels=CHANNELS, subtype="PCM_24"
    ) as out:
        for block in blocks():
            out.write(block * gain)
    return gain


def audio_rms(path: Path) -> float:
    """Report stem energy to flag uninformative instrumental-only excerpts."""
    power = 0.0
    samples = 0
    for block in sf.blocks(path, blocksize=BLOCK, dtype="float32", always_2d=True):
        power += float(np.square(block, dtype=np.float64).sum())
        samples += block.size
    return float(np.sqrt(power / samples)) if samples else 0.0


def encode_backing(source: Path, destination: Path) -> None:
    validate_audio(source)
    ffmpeg("-i", str(source), "-vn", "-c:a", "aac", "-b:a", "192k", str(destination))


def run(
    source: Path,
    output: Path,
    *,
    mode: str,
    model_dir: Path,
    karaoke_model: str = KARAOKE_MODEL,
    vocals_model: str = VOCALS_MODEL,
    backend=None,
    model_source: Path | None = None,
    worker_python: Path | None = None,
) -> dict:
    if mode not in ("direct", "two-stage"):
        raise ValueError(f"Unknown separation route: {mode}")
    output.mkdir(parents=True, exist_ok=True)
    original = output / "original.wav"
    started = time.monotonic()
    extract_audio(source, original)
    source_info = validate_audio(original)

    stages = {}
    if mode == "two-stage":
        vocals, instrumental, seconds = separate(
            original,
            output / "all-vocals",
            vocals_model,
            model_dir=model_dir,
            backend=backend,
            model_source=model_source,
            worker_python=worker_python,
            role="vocals",
        )
        stages["all_vocals_seconds"] = round(seconds, 2)
        lead, harmonies, seconds = separate(
            vocals,
            output / "lead-vocals",
            karaoke_model,
            model_dir=model_dir,
            backend=backend,
            model_source=model_source,
            worker_python=worker_python,
            role="karaoke",
        )
        gain = mix_stems((instrumental, harmonies), output / "backing.wav")
    else:
        lead, harmonies, seconds = separate(
            original,
            output / "lead-vocals",
            karaoke_model,
            model_dir=model_dir,
            backend=backend,
            model_source=model_source,
            worker_python=worker_python,
            role="karaoke",
        )
        gain = mix_stems((harmonies,), output / "backing.wav")
    stages["lead_vocals_seconds"] = round(seconds, 2)
    backing = output / "backing.wav"
    validate_audio(lead)
    backing_info = validate_audio(backing)
    if abs(source_info.frames - backing_info.frames) > SAMPLE_RATE // 10:
        raise ValueError("Backing length differs from source by more than 100 ms")
    encoded = output / "backing.m4a"
    encode_backing(backing, encoded)
    original_rms = audio_rms(original)
    target_rms = audio_rms(lead)
    report = {
        "mode": mode,
        "original_rms": round(original_rms, 5),
        "model_target_rms": round(target_rms, 5),
        "target_to_original_rms": round(target_rms / original_rms, 4)
        if original_rms
        else 0.0,
        "candidate_rms": round(audio_rms(backing), 5),
        "models": [karaoke_model]
        if mode == "direct"
        else [vocals_model, karaoke_model],
        "source_duration_seconds": round(source_info.duration, 3),
        "backing_duration_seconds": round(backing_info.duration, 3),
        "backing_duration_delta_ms": round(
            (backing_info.frames - source_info.frames) * 1000 / SAMPLE_RATE, 2
        ),
        "mix_gain": round(gain, 5),
        "encoded_bytes": encoded.stat().st_size,
        "total_seconds": round(time.monotonic() - started, 2),
        "stages": stages,
    }
    (output / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Local audio/video, never uploaded")
    parser.add_argument(
        "--output", type=Path, required=True, help="Output directory outside Git"
    )
    parser.add_argument(
        "--model-dir", type=Path, required=True, help="External model cache"
    )
    parser.add_argument(
        "--mode", choices=["direct", "two-stage", "both"], default="both"
    )
    parser.add_argument("--karaoke-model", default=KARAOKE_MODEL)
    parser.add_argument("--vocals-model", default=VOCALS_MODEL)
    parser.add_argument(
        "--model-source",
        type=Path,
        required=True,
        help="Local Kim model source tree with MelBandRoformer and demix_track",
    )
    parser.add_argument(
        "--worker-python",
        type=Path,
        default=Path(sys.executable),
        help="Python interpreter in the optional CUDA model runtime",
    )
    options = parser.parse_args()
    if not options.source.is_file():
        parser.error("Input file does not exist")
    routes = ("direct", "two-stage") if options.mode == "both" else (options.mode,)
    for mode in routes:
        report = run(
            options.source,
            options.output / mode,
            mode=mode,
            model_dir=options.model_dir,
            karaoke_model=options.karaoke_model,
            vocals_model=options.vocals_model,
            model_source=options.model_source,
            worker_python=options.worker_python,
        )
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
