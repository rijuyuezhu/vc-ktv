"""Measured packet demand for buffered playback; no probing in HTTP handlers."""

import json
import math
import os
import subprocess
from collections import defaultdict
from pathlib import Path

from vc_ktv.core.artifacts import meets_measured_budget

# A five-second buffer should absorb shorter bursts, not sustained overload.
WINDOW_SECONDS = 5


def packet_peaks(csv: str) -> dict[str, float] | None:
    bins: dict[int, int] = defaultdict(int)
    for line in csv.splitlines():
        fields = line.split(",")
        if len(fields) < 2:
            continue
        try:
            timestamp, size = float(fields[0]), int(fields[1])
            if math.isfinite(timestamp) and size >= 0:
                bins[math.floor(timestamp)] += size
        except ValueError:
            continue
    if not bins:
        return None
    # Only windows touching a nonempty bin can be maximal. Avoid allocating
    # arrays proportional to arbitrary media timestamps.
    peak = max(bins.values())
    sustained = max(
        sum(bins.get(start + j, 0) for j in range(WINDOW_SECONDS))
        for t in bins
        for start in range(t - WINDOW_SECONDS + 1, t + 1)
    )
    return {
        "peak_1s_mbps": peak * 8 / 1e6,
        "peak_5s_mbps": sustained * 8 / (WINDOW_SECONDS * 1e6),
    }


def measure(path: Path) -> dict[str, float] | None:
    try:
        result = subprocess.run(
            [
                os.environ.get("KTV_FFPROBE", "ffprobe"),
                "-v",
                "error",
                "-show_packets",
                "-show_entries",
                "packet=pts_time,size",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return packet_peaks(result.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def read_source_profile(source: Path, archive: Path) -> dict | None:
    try:
        info = json.loads((archive / "source-bitrate.json").read_text())
        stat = source.stat()
        if (info["source_file"], info["size"], info["mtime_ns"]) == (
            source.name,
            stat.st_size,
            stat.st_mtime_ns,
        ):
            peaks = {key: float(info[key]) for key in ("peak_1s_mbps", "peak_5s_mbps")}
            if all(math.isfinite(value) and value >= 0 for value in peaks.values()):
                return peaks
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def source_profile(store, source: Path, archive: Path) -> dict | None:
    """Use the published immutable source record, falling back to legacy sidecars."""
    meta = store.artifact(source.name, "source")
    if all(key in meta for key in ("peak_1s_mbps", "peak_5s_mbps")):
        return {key: meta[key] for key in ("peak_1s_mbps", "peak_5s_mbps")}
    return read_source_profile(source, archive)


def fits_budget(
    path: Path, duration: float, budget: float, profile: dict | None
) -> bool:
    if duration <= 0:
        return False
    return meets_measured_budget(
        {**(profile or {}), "average_mbps": path.stat().st_size * 8 / (duration * 1e6)},
        budget,
        headroom=0.8,
        allow_unknown_peaks=True,
    )
