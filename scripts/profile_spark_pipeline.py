#!/usr/bin/env python3
"""Isolated, low-overhead performance measurements for the adopted GPU pipeline.

Outputs are never stored in the event database. Each attempt uses a fresh
working directory, while caller-provided media and confirmed lyrics are read-only.
"""

import argparse
import hashlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import vc_ktv.upstream_pipeline as upstream


def clock() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(data)
    return checksum.hexdigest()


def gpu_info() -> dict:
    try:
        output = (
            subprocess.check_output(
                [
                    "/usr/lib/wsl/lib/nvidia-smi",
                    "--query-gpu=memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                timeout=4,
            )
            .strip()
            .split(",")
        )
        return {"gpu_mib": int(output[0]), "gpu_util": int(output[1])}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {"gpu_mib": None, "gpu_util": None}


def process_info() -> dict:
    """Approximate descendant RSS/CPU from ps; sampling every five seconds."""
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,rss=,%cpu="], text=True, timeout=4
        )
        rows = []
        for line in output.splitlines():
            try:
                pid, ppid, rss, cpu = line.split()
                rows.append((int(pid), int(ppid), int(rss), float(cpu)))
            except (ValueError, TypeError):
                continue
        family = {os.getpid()}
        for _ in range(8):
            new = family | {pid for pid, ppid, *_ in rows if ppid in family}
            if family == new:
                break
            family = new
        selected = [r for r in rows if r[0] in family]
        return {
            "rss_mib": round(sum(r[2] for r in selected) / 1024, 1),
            "process_cpu_pct": round(sum(r[3] for r in selected), 1),
            "process_count": len(selected),
        }
    except (OSError, subprocess.SubprocessError):
        return {"rss_mib": None, "process_cpu_pct": None, "process_count": None}


def run_profile(
    source: Path,
    lyrics: str,
    target: Path,
    *,
    interval: float = 5.0,
    tifa_root: Path | None = None,
    mode: str = "quality",
) -> dict:
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite existing run {target}")
    target.mkdir(parents=True)
    events: list[dict] = []
    measurements: list[dict] = []
    stop_sampling = threading.Event()
    started = time.monotonic()

    def record(name, seconds=None, *, success=True):
        item = {
            "time": clock(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "name": name,
        }
        if seconds is not None:
            item["duration_s"] = round(seconds, 3)
            item["success"] = success
        events.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    # Each top-level stage is wrapped in the profiler process only. We do not
    # instrument model loops, log individual frames or mutate vendored source.
    replacements = {}
    for label, symbol in [
        ("extract", "extract"),
        ("kim", "prepare_alignment_audio"),
        ("qwen_tifa", "align_reliable_blocks"),
        ("completion", "complete_timeline"),
        ("ass", "write_bundle"),
        ("ffmpeg", "render"),
    ]:
        original = getattr(upstream, symbol)
        replacements[symbol] = original

        def instrument(*args, _original=original, _label=label, **kwargs):
            begin = time.monotonic()
            try:
                result = _original(*args, **kwargs)
            except BaseException:
                record(_label, time.monotonic() - begin, success=False)
                raise
            record(_label, time.monotonic() - begin)
            return result

        setattr(upstream, symbol, instrument)

    def sample():
        while not stop_sampling.is_set():
            measurements.append(
                {
                    "time": clock(),
                    "elapsed_s": round(time.monotonic() - started, 3),
                    **gpu_info(),
                    **process_info(),
                }
            )
            stop_sampling.wait(interval)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    status = "failed"
    try:
        print("RUN", target, "start", clock(), flush=True)
        result = upstream.make_karaoke_video(
            source,
            lyrics,
            target / "work",
            upstream.spark_settings(tifa_root=tifa_root),
            progress=lambda msg: record("event: " + msg),
            **({"mode": mode} if mode != "quality" else {}),
        )
        status = "done"
        record("complete")
        return result.as_dict()
    finally:
        stop_sampling.set()
        # sampler may need to finish nvidia-smi; keep bounded
        sampler.join(timeout=interval + 5)
        for key, original in replacements.items():
            setattr(upstream, key, original)
        data = {
            "status": status,
            "source_sha256": digest(source),
            "source_size": source.stat().st_size,
            "lyric_line_count": len(lyrics.splitlines()),
            "started": events[0]["time"] if events else clock(),
            "duration_s": round(time.monotonic() - started, 3),
            "events": events,
            "resources": measurements,
        }
        (target / "metrics.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--lyrics", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--name", default="cold")
    parser.add_argument(
        "--mode",
        choices=("global-kim", "global", "quality", "fast"),
        default="global-kim",
    )
    parser.add_argument(
        "--tifa-root",
        type=Path,
        default=Path.home() / "Code/vc-ktv",
        help="Root of existing verified read-only .runtime/TIFA environment",
    )
    args = parser.parse_args()
    source = args.source.expanduser().resolve(strict=True)
    lyrics = args.lyrics.expanduser().resolve(strict=True).read_text(encoding="utf-8")
    if args.name not in {"cold", "cold-retry", "cold-fixed", "second"}:
        parser.error("--name must be cold, cold-retry, cold-fixed or second")
    result = run_profile(
        source,
        lyrics,
        args.out / args.name,
        tifa_root=args.tifa_root.expanduser().resolve(strict=True),
        mode=args.mode,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
