"""Measured packet demand for buffered playback; no probing in HTTP handlers."""

import json
import logging
import math
import os
import subprocess
from collections import defaultdict
from pathlib import Path

# A five-second buffer should absorb shorter bursts, not sustained overload.
log = logging.getLogger(__name__)

WINDOW_SECONDS = 5
ONE_SECOND_BURST_FACTOR = 1.5


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


def record_source_profile(source: Path, archive: Path) -> None:
    """Persist immutable source statistics next to the preparation manifest."""
    stats = measure(source)
    if stats is None:
        return
    temporary = archive / "source-bitrate.tmp.json"
    try:
        identity = source.stat()
        temporary.write_text(
            json.dumps(
                {
                    "source_file": source.name,
                    "size": identity.st_size,
                    "mtime_ns": identity.st_mtime_ns,
                    **stats,
                }
            )
        )
        temporary.replace(archive / "source-bitrate.json")
    except OSError:
        log.warning("Unable to persist source bitrate profile")
    finally:
        temporary.unlink(missing_ok=True)


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


def fits_budget(
    path: Path, duration: float, budget: float, profile: dict | None
) -> bool:
    """Preserve the existing 20% average headroom plus a short-window policy.

    Legacy/unmeasurable files retain the historical average-only decision.
    The budget is a playback buffer target, not a transport rate limiter.
    """
    if duration <= 0 or path.stat().st_size * 8 > duration * budget * 800_000:
        return False
    if profile is None:
        return True
    return (
        profile["peak_1s_mbps"] <= budget * ONE_SECOND_BURST_FACTOR
        and profile["peak_5s_mbps"] <= budget
    )
