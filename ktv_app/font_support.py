"""Best-effort fontconfig diagnostics for ASS exports; never change lyric text."""

import bisect
import re
import subprocess
import unicodedata
from functools import lru_cache


def _fontconfig(*args: str) -> str | None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=True, timeout=5
        )
        return result.stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def primary_font_warning(font: str) -> str | None:
    """Check the actual bold family chosen by libass's fontconfig provider."""
    family = _fontconfig("fc-match", "-f", "%{family}\n", f"{font}:style=Bold")
    if family is None:
        return "无法执行 Fontconfig 字体检查（fc-match）；请安装 fontconfig。"
    if font.casefold() not in {name.strip().casefold() for name in family.split(",")}:
        return (
            f"字体 {font!r} 未安装（Bold）；Fontconfig 当前回退到 {family.strip()!r}。"
        )
    return None


@lru_cache(maxsize=1)
def _installed_ranges() -> tuple[tuple[int, int], ...] | None:
    """Query all installed font glyph ranges once, without new Python deps."""
    output = _fontconfig("fc-list", "-f", "%{family}|%{charset}\n")
    if output is None:
        return None
    ranges = []
    for entry in output.splitlines():
        family, sep, charset = entry.partition("|")
        # Bitmap/color emoji fonts can be listed by Fontconfig while libass
        # cannot use their glyphs (verified with Noto Color Emoji + U+1F3B5).
        if not sep or "color emoji" in family.casefold():
            continue
        for value in charset.split():
            if not re.fullmatch(r"[0-9a-fA-F]+(?:-[0-9a-fA-F]+)?", value):
                continue
            begin, _, end = value.partition("-")
            ranges.append((int(begin, 16), int(end or begin, 16)))
    if not ranges:
        return None
    merged: list[list[int]] = []
    for begin, end in sorted(ranges):
        if merged and begin <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    return tuple((begin, end) for begin, end in merged)


def font_diagnostics(font: str, texts: list[str]) -> dict:
    """Report unavailable glyphs, rather than silently treating rendering as complete."""
    warning = primary_font_warning(font)
    ranges = _installed_ranges() if texts else ()
    issues = [warning] if warning else []
    if texts and ranges is None:
        issues.append("无法检查已安装字体的字形覆盖（fc-list 不可用）。")
    missing: list[str] = []
    if ranges:
        starts = [a for a, _ in ranges]
        for ch in sorted(set("".join(texts)), key=ord):
            # Zero-width format codes, whitespace, selectors and combining marks
            # do not require standalone visible glyphs.
            if ch.isspace() or unicodedata.category(ch)[0] in "CM":
                continue
            point = ord(ch)
            i = bisect.bisect_right(starts, point) - 1
            if i < 0 or point > ranges[i][1]:
                missing.append(ch)
    if missing:
        issues.append(
            f"ASS 可用字体缺少 {len(missing)} 种歌词字符，可能显示为方框："
            + " ".join(f"{c}(U+{ord(c):04X})" for c in missing[:12])
            + (" …" if len(missing) > 12 else "")
        )
    return {
        "requested_font": font,
        "fontconfig_checked": ranges is not None if texts else warning is None,
        "missing_glyph_count": len(missing),
        "missing_glyphs": [f"U+{ord(ch):04X}" for ch in missing[:20]],
        "warnings": issues,
    }
