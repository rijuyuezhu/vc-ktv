#!/usr/bin/env python3
"""Opt-in, isolated full-song TIFA baseline. Never uses the live KTV database."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from vc_ktv.autoprep import pronunciation_lyrics
from vc_ktv.textgrid_import import convert_textgrid


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(args: argparse.Namespace) -> dict:
    audio = args.audio.resolve(strict=True)
    lyrics_file = args.lyrics.resolve(strict=True)
    tifa = args.tifa.resolve(strict=True)
    model = args.model.resolve(strict=True)
    python = args.python.resolve(strict=True)
    out = args.out.resolve()
    if not args.confirm_rights:
        raise ValueError(
            "Only authorized recordings may be used: pass --confirm-rights"
        )
    if (
        not audio.is_file()
        or audio.suffix.lower() != ".wav"
        or not lyrics_file.is_file()
    ):
        raise ValueError("Supply an existing WAV and a UTF-8 plain lyric file")
    if not (tifa / "infer.py").is_file() or not model.is_file() or not python.is_file():
        raise ValueError("TIFA source, frozen weight or Python executable is missing")
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite an existing experiment: {out}")
    # Guard the input checkout and model directory against accidental output writes.
    if any(out == p or out.is_relative_to(p) for p in (tifa, model.parent)):
        raise ValueError("Output must be outside the model and TIFA source trees")

    original = lyrics_file.read_text(encoding="utf-8")
    if not original.strip():
        raise ValueError("Lyrics are empty")
    if args.mode == "pronunciation":
        transcript, mappings, substitutions = pronunciation_lyrics(original)
    else:
        transcript, mappings, substitutions = original, None, []
    out.mkdir(parents=True)
    # TIFA looks for a same-stem .txt beside the WAV. Never write a lyric
    # file next to the input (which may be read-only or shared).
    working_audio = out / "audio.wav"
    shutil.copyfile(audio, working_audio)
    (out / "lyrics.original.txt").write_text(original, encoding="utf-8")
    (out / "audio.txt").write_text(transcript, encoding="utf-8")
    (out / "pronunciation_trace.json").write_text(
        json.dumps(substitutions, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    grid_dir = out / "alignment"
    grid_dir.mkdir()
    command = [
        str(python),
        str(tifa / "infer.py"),
        str(working_audio),
        "-m",
        str(model),
        "-l",
        args.language,
        "-o",
        str(grid_dir),
        "--batch-size",
        "1",
        "--num-workers",
        "0",
        "--stat",
    ]
    info = {
        "schema": 1,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "audio": str(audio),
        "audio_sha256": sha256(audio),
        "lyrics_sha256": sha256(lyrics_file),
        "source_tifa_dir": str(tifa),
        "tifa_commit": subprocess.run(
            ["git", "-C", str(tifa), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        or None,
        "model": str(model),
        "model_sha256": sha256(model),
        "command": command,
        "elapsed_seconds": None,
        "returncode": None,
        "textgrid_exists": False,
        "alignment_report": None,
        "manually_labelled_accuracy": False,
        "gpu_memory_peak_mib": None,
        "error": None,
    }
    try:
        start = time.perf_counter()
        with (out / "tifa.log").open("w") as log:
            result = subprocess.run(
                command,
                cwd=tifa,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
                check=False,
            )
        info["elapsed_seconds"] = round(time.perf_counter() - start, 3)
        info["returncode"] = result.returncode
        grid = grid_dir / "audio.TextGrid"
        info["textgrid_exists"] = grid.is_file()
        if result.returncode:
            info["error"] = "tifa_process_nonzero"
        elif not grid.is_file():
            text = (out / "tifa.log").read_text(encoding="utf-8", errors="replace")
            info["error"] = (
                "alignment_oov"
                if "Unknown phonemes" in text and "Skipping" in text
                else "tifa_missing_output"
            )
        else:
            from textgrid import TextGrid

            duration = TextGrid.fromFile(str(grid)).maxTime
            lrc, report = convert_textgrid(
                grid,
                original,
                duration,
                pronunciation=transcript if mappings is not None else None,
                source_spans=mappings,
            )
            (out / "aligned.lrc").write_text(lrc, encoding="utf-8")
            info["alignment_report"] = report
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        (out / "result.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--lyrics", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--tifa", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--mode", choices=("raw", "pronunciation"), default="raw")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--confirm-rights", action="store_true")
    args = parser.parse_args()
    result = run(args)
    print(
        json.dumps(
            {
                "result": str(args.out / "result.json"),
                "returncode": result["returncode"],
                "error": result["error"],
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["error"] is None else 1)


if __name__ == "__main__":
    main()
