"""Media properties measured by workers, not queue polling."""

import hashlib
import json
import math
import os
import subprocess
from pathlib import Path

from vc_ktv.processing.bitrate import measure


def inspect_media(path: Path, *, measure_bitrate: bool = True) -> dict:
    try:
        result = subprocess.run(
            [
                os.environ.get("KTV_FFPROBE", "ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,height,start_time,duration:format=duration,start_time",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        data = json.loads(result.stdout)
        video = next(s for s in data["streams"] if s.get("codec_type") == "video")
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            return {}
        return {
            "codec": video["codec_name"],
            "height": int(video["height"]),
            "video_start": float(video.get("start_time", 0)),
            "duration": duration,
            "average_mbps": path.stat().st_size * 8 / (duration * 1e6),
            **((measure(path) or {}) if measure_bitrate else {}),
        }
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        StopIteration,
        subprocess.SubprocessError,
    ):
        return {}


def known_media(path: Path, metadata: dict) -> dict:
    """Published filenames are immutable; reuse complete measurements."""
    fields = (
        "codec",
        "height",
        "duration",
        "video_start",
        "average_mbps",
        "peak_1s_mbps",
        "peak_5s_mbps",
    )
    return (
        metadata
        if all(key in metadata for key in fields)
        else {**metadata, **inspect_media(path)}
    )


def audio_identity(path: Path) -> str | None:
    """Exact audio packet data AND presentation timestamps, not a title match.

    Includes all audio streams. Video codec-only changes can retain this key;
    a new soundtrack, trim or timestamp shift cannot silently reuse alignment.
    """
    try:
        result = subprocess.run(
            [
                os.environ.get("KTV_FFPROBE", "ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_packets",
                "-show_data_hash",
                "sha256",
                "-show_entries",
                "packet=stream_index,pts_time,duration_time,data_hash",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        if result.stdout and b"SHA256:" in result.stdout:
            return hashlib.sha256(result.stdout).hexdigest()
    except (OSError, subprocess.SubprocessError):
        pass
    return None
