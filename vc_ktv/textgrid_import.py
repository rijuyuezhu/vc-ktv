"""Strict, reviewable TIFA TextGrid -> enhanced LRC conversion.

This only imports an existing TextGrid, never claims to have run a model.
Semantic word boundaries are not fabricated into per-character times.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from textgrid import TextGrid


def chars(text: str) -> tuple[str, list[int]]:
    content, positions = [], []
    for i, char in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", char).casefold():
            if normalized.isalnum():
                content.append(normalized)
                positions.append(i)
    return "".join(content), positions


def stamp(value: float, *, milliseconds: bool = False) -> str:
    scale = 1000 if milliseconds else 100
    n = round(value * scale)
    return f"{n // (60 * scale):02d}:{n // scale % 60:02d}.{n % scale:0{3 if milliseconds else 2}d}"


def convert_textgrid(
    path: Path,
    original_lyrics: str,
    duration: float,
    offset: float = 0,
    *,
    pronunciation: str | None = None,
    source_spans: list[list[tuple[int, int]]] | None = None,
    millisecond_timestamps: bool = False,
) -> tuple[str, dict]:
    if not -10 <= offset <= 10:
        raise ValueError("时间偏移必须在 -10 至 10 秒以内")
    lines = [s.strip() for s in original_lyrics.splitlines() if s.strip()]
    if not lines or len(lines) > 800:
        raise ValueError("请先确认不超过 800 行的纯文本歌词")
    if any(s.startswith("[") and "]" in s for s in lines):
        raise ValueError("请导入纯文本歌词；已有时间戳的 LRC 请直接制作字幕")
    spoken = (
        [s.strip() for s in pronunciation.splitlines() if s.strip()]
        if pronunciation is not None
        else lines
    )
    if len(spoken) != len(lines):
        raise ValueError("发音文本与显示歌词的行数不一致")
    if source_spans is None:
        source_spans = [[(i, i + 1) for i in range(len(line))] for line in lines]
    if len(source_spans) != len(lines) or any(
        len(mapping) != len(reading)
        or any(not 0 <= a < b <= len(display) for a, b in mapping)
        or any(a1 > a2 or b1 > b2 for (a1, b1), (a2, b2) in zip(mapping, mapping[1:]))
        for display, reading, mapping in zip(lines, spoken, source_spans)
    ):
        raise ValueError("发音文本与原文字符映射无效")
    grid = TextGrid.fromFile(str(path))
    tier = next((x for x in grid if x.name == "texts"), None)
    if tier is None:
        raise ValueError("TextGrid 缺少 TIFA 的 texts tier")
    units = [
        (str(x.mark).strip(), x.minTime + offset, x.maxTime + offset)
        for x in tier
        if str(x.mark).strip()
    ]
    if not units:
        raise ValueError("TextGrid 的 texts tier 没有有效文本区间")
    for item in units:
        if not 0 <= item[1] < item[2] <= duration + 0.02:
            raise ValueError("对齐区间超出了视频范围")
    if any(a[2] > b[1] + 0.02 for a, b in zip(units, units[1:])):
        raise ValueError("对齐区间存在重叠或时间倒退")

    total = "".join(chars(line)[0] for line in spoken)
    aligned = "".join(chars(unit[0])[0] for unit in units)
    if total != aligned:
        raise ValueError("对齐文本与确认歌词不完全匹配；请核对歌词版本，不进行猜测补齐")

    output = []
    unit_index = 0
    for line, reading, mapping in zip(lines, spoken, source_spans):
        normalized, positions = chars(reading)
        if not normalized:
            raise ValueError("歌词含只有标点的行，需先人工清理")
        cursor, original_cursor, converted = 0, 0, []
        first_unit = unit_index
        while cursor < len(normalized):
            if unit_index >= len(units):
                raise ValueError("TextGrid 缺少后续歌词区间")
            token, start, end = units[unit_index]
            key = chars(token)[0]
            if not key or not normalized.startswith(key, cursor):
                raise ValueError("TextGrid 单元跨越歌词行或与原词边界不符")
            end_cursor = cursor + len(key)
            mapped = [mapping[positions[i]] for i in range(cursor, end_cursor)]
            display_start = min(a for a, _ in mapped)
            display_end = max(b for _, b in mapped)
            if display_start < original_cursor:
                # Several native TIFA units can refer to the same scat word.
                # Keep their genuine outer acoustic bounds, not fake timing
                # for the letters of that displayed word.
                if not converted:
                    raise ValueError("发音映射发生越界")
                first, _, prior = converted.pop()
                converted.append(
                    (first, end, prior + line[original_cursor:display_end])
                )
            else:
                converted.append((start, end, line[original_cursor:display_end]))
            cursor, original_cursor = end_cursor, max(original_cursor, display_end)
            unit_index += 1
        if original_cursor < len(line):
            # trailing punctuation is part of the preceding semantic word.
            start, end, display = converted.pop()
            converted.append((start, end, display + line[original_cursor:]))
        line_start = units[first_unit][1]
        output.append(
            f"[{stamp(line_start, milliseconds=millisecond_timestamps)}]"
            + "".join(
                f"<{stamp(start, milliseconds=millisecond_timestamps)}>{display}"
                f"<{stamp(end, milliseconds=millisecond_timestamps)}>"
                for start, end, display in converted
            )
        )
    if unit_index != len(units):
        raise ValueError("TextGrid 中存在多余的语义词")
    return "\n".join(output), {
        "source": "TIFA TextGrid import",
        "lines": len(output),
        "semantic_units": unit_index,
        "offset_seconds": offset,
        "verified_by_model": False,
        "review_required": True,
    }
