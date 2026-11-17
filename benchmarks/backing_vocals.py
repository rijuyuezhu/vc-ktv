"""Offline lead-vocal removal POC; never used by the live preparation pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from pathlib import Path

import numpy as np
import soundfile as sf

KARAOKE_MODEL = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
VOCALS_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
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
    backend=None,
) -> tuple[Path, Path, float]:
    """Return model's Vocals and Instrumental stems, in that order.

    For the karaoke model Vocals means *lead*, Instrumental means *everything
    except lead* (including backing voices). For a normal vocal model Vocals
    means *all* voices, Instrumental means instruments.
    """
    if backend is None:
        from audio_separator.separator import Separator

        backend = Separator
    stage.mkdir(parents=True, exist_ok=True)
    separator = backend(
        model_file_dir=str(model_dir),
        output_dir=str(stage),
        output_format="WAV",
        sample_rate=SAMPLE_RATE,
        normalization_threshold=1.0,
        amplification_threshold=0.0,
        log_level=logging.WARNING,
    )
    started = time.monotonic()
    separator.load_model(model_filename=model)
    names = {"Vocals": "vocals", "Instrumental": "other"}
    outputs = separator.separate(str(source), names)
    elapsed = time.monotonic() - started
    expected = (stage / "vocals.wav", stage / "other.wav")
    reported = {Path(name).name for name in outputs}
    if reported != {path.name for path in expected}:
        raise RuntimeError(f"Unexpected separation stems: {sorted(reported)}")
    for path in expected:
        validate_audio(path)
    return *expected, elapsed


def _read(reader: sf.SoundFile, length: int) -> np.ndarray:
    data = reader.read(length, dtype="float32", always_2d=True)
    if len(data) == length:
        return data
    return np.pad(data, ((0, length - len(data)), (0, 0)))


def combine_instruments_and_harmonies(
    instruments: Path, harmonies: Path, destination: Path
) -> float:
    """Mix without stem rescaling; protect against clipping in fixed-point output."""
    first, second = validate_audio(instruments), validate_audio(harmonies)
    if abs(first.frames - second.frames) > SAMPLE_RATE // 10:
        raise ValueError("Stem length differs by more than 100 ms")

    peak = 0.0
    with sf.SoundFile(instruments) as a, sf.SoundFile(harmonies) as b:
        for start in range(0, first.frames, BLOCK):
            mixed = _read(a, min(BLOCK, first.frames - start)) + _read(
                b, min(BLOCK, first.frames - start)
            )
            peak = max(peak, float(np.max(np.abs(mixed))))
    gain = min(1.0, 0.99 / peak) if peak else 1.0

    with (
        sf.SoundFile(instruments) as a,
        sf.SoundFile(harmonies) as b,
        sf.SoundFile(
            destination,
            "w",
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            subtype="PCM_24",
        ) as out,
    ):
        for start in range(0, first.frames, BLOCK):
            length = min(BLOCK, first.frames - start)
            out.write((_read(a, length) + _read(b, length)) * gain)
    return gain


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
        )
        stages["all_vocals_seconds"] = round(seconds, 2)
        lead, harmonies, seconds = separate(
            vocals,
            output / "lead-vocals",
            karaoke_model,
            model_dir=model_dir,
            backend=backend,
        )
        gain = combine_instruments_and_harmonies(
            instrumental, harmonies, output / "backing.wav"
        )
    else:
        lead, harmonies, seconds = separate(
            original,
            output / "lead-vocals",
            karaoke_model,
            model_dir=model_dir,
            backend=backend,
        )
        gain = 1.0
        # The one-pass model's Instrumental stem already contains harmonies.
        # The separately named path is the model output, not a synthetic remix.
    stages["lead_vocals_seconds"] = round(seconds, 2)
    backing = output / (
        "backing.wav" if mode == "two-stage" else "lead-vocals/other.wav"
    )
    validate_audio(lead)
    backing_info = validate_audio(backing)
    if abs(source_info.frames - backing_info.frames) > SAMPLE_RATE // 10:
        raise ValueError("Backing length differs from source by more than 100 ms")
    encoded = output / "backing.m4a"
    encode_backing(backing, encoded)
    report = {
        "mode": mode,
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
        )
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
