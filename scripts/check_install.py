"""Check local prerequisites without downloading models or accessing event data."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def probe(command: list[str]) -> str:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def checks(mode: str, config: Path, pipeline: Path) -> list[tuple[bool, str]]:
    results = [(sys.version_info >= (3, 12), "Python >= 3.12")]
    for program in ("ffmpeg", "ffprobe", "fc-match"):
        results.append((shutil.which(program) is not None, f"{program} on PATH"))
    if shutil.which("ffmpeg"):
        results.extend(
            [
                (
                    " subtitles " in probe(["ffmpeg", "-hide_banner", "-filters"]),
                    "FFmpeg subtitles/libass filter",
                ),
                (
                    "libx264" in probe(["ffmpeg", "-hide_banner", "-encoders"]),
                    "FFmpeg libx264 encoder",
                ),
            ]
        )
    if shutil.which("fc-match"):
        family = probe(["fc-match", "-f", "%{family}", "Noto Sans CJK SC:style=Bold"])
        results.append(("Noto Sans CJK" in family, "Noto Sans CJK font"))
    for name in ("admin-password", "token-secret"):
        results.append(((config / name).is_file(), f"config/{name}"))
    if mode == "gpu":
        for relative in (
            "models/forced_alignment/sources/TIFA/infer.py",
            "models/forced_alignment/tifa/TIFA-1.0-ST/model.pt",
            "models/separation/sources/Kim",
            "models/separation/Kim/MelBandRoformer.ckpt",
            "models/Qwen3-ASR-1.7B/config.json",
            "models/Qwen3-ASR-1.7B/model.safetensors.index.json",
            "runtime/tifa/bin/python",
            "runtime/kim/bin/python",
            "runtime/qwen/bin/python",
        ):
            path = pipeline / relative
            results.append(
                (
                    path.is_dir() if relative.endswith("/Kim") else path.is_file(),
                    f"pipeline/{relative}",
                )
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("base", "gpu"), default="base")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.getenv("VC_KTV_CONFIG", "~/.config/vc-ktv")).expanduser(),
    )
    parser.add_argument(
        "--pipeline",
        type=Path,
        default=Path(
            os.getenv("VC_KTV_PIPELINE_ROOT", "~/.local/share/vc-ktv/pipeline")
        ).expanduser(),
    )
    args = parser.parse_args()
    results = checks(args.mode, args.config.expanduser(), args.pipeline.expanduser())
    for ok, name in results:
        print(f"{'OK' if ok else 'MISSING'}  {name}")
    if args.mode == "gpu":
        print("GPU checks only verify local files; CUDA imports and actual inference")
        print("must still be validated with an isolated test video.")
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
