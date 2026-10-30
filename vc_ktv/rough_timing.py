"""Last-resort vocal-range-based subtitle timing, never a reason to drop media.

This deliberately does not attempt to infer real alignments. It preserves every
confirmed lyric line in order if ASR/TIFA, scat G2P, or TextGrid conversion fails.
"""

import math
from pathlib import Path

import numpy as np
import soundfile as sf

from ktv_app.timeline import Line, Timeline, Unit


def vocal_range(audio: Path | None, duration: float) -> tuple[float, float]:
    """Best-effort vocal onset/offset; return song-wide bounds on any failure."""
    fallback = (0.0, duration)
    if audio is None or not Path(audio).is_file():
        return fallback
    try:
        frames = []
        with sf.SoundFile(audio) as sound:
            rate = sound.samplerate
            step = max(1024, rate // 4)
            for data in sound.blocks(blocksize=step, dtype="float32", always_2d=True):
                frames.append(float(np.sqrt(np.mean(data.astype("float64") ** 2))))
        if len(frames) < 8:
            return fallback
        power = np.asarray(frames)
        peak = float(np.percentile(power, 95))
        if not math.isfinite(peak) or peak < 1e-5:
            return fallback
        active = np.flatnonzero(power > max(peak * 0.11, 0.0002))
        if len(active) < 8:
            return fallback
        step_seconds = step / rate
        left = max(0.0, min(duration, float(active[0]) * step_seconds))
        right = max(0.0, min(duration, (float(active[-1]) + 1) * step_seconds))
        if right - left < duration * 0.3:
            return fallback
        return left, right
    except (OSError, ValueError, RuntimeError, sf.LibsndfileError):
        return fallback


def approximate_timeline(
    lyrics: str, duration: float, *, audio: Path | None = None, reason: str = ""
) -> Timeline:
    """Fit all lines monotonically in the detected vocal range, no hard error."""
    source = [line.strip() for line in lyrics.splitlines() if line.strip()]
    if not source:
        raise ValueError("字幕缺少歌词")
    left, right = vocal_range(audio, duration)
    # Song may have instrumental intro/outro; avoid squeezing all words into
    # the same region. Distribute monotonically, weighted by displayed length.
    if right - left < 0.1:
        left, right = 0.0, duration
    weights = [max(3.0, min(28.0, len(line) ** 0.8)) for line in source]
    total = sum(weights)
    cursor = left
    min_gap = min(0.01, (right - left) / (len(source) + 1) * 0.35)
    cumulative = 0.0
    lines = []
    for i, (text, weight) in enumerate(zip(source, weights)):
        cumulative += weight
        end = (
            right
            if i == len(source) - 1
            else left + (right - left) * cumulative / total
        )
        end = max(cursor + min_gap, end)
        end = min(duration, end)
        unit = Unit(
            id=f"rough-unit-{i}",
            text=text,
            source_start=0,
            source_end=len(text),
            start=cursor,
            end=end,
            timing_source="estimated",
            flags=["rough_timing"],
        )
        lines.append(
            Line(
                id=f"rough-line-{i}",
                text=text,
                source_line=i,
                start=cursor,
                end=end,
                units=[unit],
                flags=["rough_timing"],
            )
        )
        cursor = end
    return Timeline(
        duration=duration,
        lines=lines,
        diagnostics={
            "pipeline": "last_resort_rough_timing",
            "estimated_lines": len(lines),
            "warning": "粗略时间轴供试听，不代表模型已逐字对齐",
            "failure": reason[:350],
            "vocal_range": [round(left, 2), round(right, 2)],
        },
    )
