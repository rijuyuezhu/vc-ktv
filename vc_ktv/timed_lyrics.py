"""Validate optional provider line timing without claiming acoustic alignment."""

from __future__ import annotations

import re

from ktv_app.text_normalization import LEADING_CREDIT, clean_lyric_text

from .karaoke import LINE_STAMP, parse_lrc, seconds


def provider_timeline(raw: str, confirmed: str) -> str | None:
    """Return canonical line-timed LRC only if every confirmed lyric line is timed.

    Untimed metadata/credits may be omitted. An untimed *sung* line, a
    malformed sequence, or different lyrics must NOT silently gain timings.
    """
    if not raw or len(raw) > 24_000 or not confirmed:
        return None
    offsets = re.findall(r"\[offset\s*:\s*([+-]?\d+)\s*\]", raw, re.I)
    if any(int(value) != 0 for value in offsets):
        return None  # Provider-global shift needs its own explicit calibration.
    try:
        records = parse_lrc(raw)
    except ValueError:
        return None
    if len(records) < 2 or len(records) > 400:
        return None
    # Disallow reordering/duplicate source timestamps; no guessing which verse.
    starts = []
    for line in raw.splitlines():
        text = line.strip()
        while match := LINE_STAMP.match(text):
            starts.append(_timestamp(match.group(1)))
            text = text[match.end() :]
    if len(starts) != len(records) or any(b <= a for a, b in zip(starts, starts[1:])):
        return None
    cleaned = clean_lyric_text(raw)
    if cleaned != confirmed:
        return None
    started, normalized = False, []
    for row in records:
        value = row["text"].strip()
        if not started and LEADING_CREDIT.match(value):
            continue
        started = True
        if not value or row["units"]:
            return None  # Only reliable line-level LRC; YRC is a separate future mode.
        normalized.append(f"[{_stamp(row['start'])}]{value}")
    if (
        len(normalized) < 2
        or "\n".join(line.split("]", 1)[1] for line in normalized) != confirmed
    ):
        return None
    return "\n".join(normalized)


def project_provider_timeline(raw: str, original_text: str, selected: list[int]) -> str:
    """Project selected source lines onto their actual LRC stamps, never retime them."""
    all_lines = clean_lyric_text(raw, keep_leading_credits=True).splitlines()
    if all_lines != original_text.splitlines():
        raise ValueError("校正歌词与原始来源不匹配")
    stamped = []
    for raw_line in raw.splitlines():
        display = clean_lyric_text(raw_line, keep_leading_credits=True)
        if not display:
            continue
        rows = parse_lrc(raw_line) if LINE_STAMP.match(raw_line.strip()) else []
        if len(rows) > 1 or (rows and (rows[0]["text"] != display or rows[0]["units"])):
            raise ValueError("来源歌词包含无法校正的时间标签")
        stamped.append(rows[0]["start"] if rows else None)
    if len(stamped) != len(all_lines):
        raise ValueError("来源歌词行数与时间轴不匹配")
    if any(stamped[index] is None for index in selected):
        raise ValueError("保留的歌词中有无时间戳行，请改用普通歌词对齐")
    return "\n".join(
        f"[{_stamp(stamped[index])}]{all_lines[index]}" for index in selected
    )


def _timestamp(stamp: str) -> float:
    match = re.fullmatch(r"(\d{1,3}):([0-5]\d)(?:\.(\d{1,3}))?", stamp)
    if not match:
        raise ValueError("invalid timestamp")
    return seconds(match.groups())


def _stamp(seconds: float) -> str:
    value = round(seconds * 100)
    return f"{value // 6000:02d}:{value // 100 % 60:02d}.{value % 100:02d}"


def offset_timeline(timed_lrc: str, offset_ms: int, duration: float) -> str:
    """Reject clip/version mismatch outside source media; no silent clamp."""
    if not -60_000 <= offset_ms <= 60_000:
        raise ValueError("偏移只支持 -60 到 60 秒")
    records = parse_lrc(timed_lrc)
    if len(records) < 2 or any(row["units"] for row in records):
        raise ValueError("来源需要至少两行逐句时间轴")
    offset = offset_ms / 1000
    times = [row["start"] + offset for row in records]
    if any(t < 0 or t >= duration for t in times):
        raise ValueError("来源 LRC 时间轴超出视频边界；请检查翻唱版本和偏移")
    if any(b - a < 0.05 for a, b in zip(times, times[1:])):
        raise ValueError("来源 LRC 时间戳重复、倒退或过于密集")
    return "\n".join(f"[{_stamp(t)}]{row['text']}" for t, row in zip(times, records))
