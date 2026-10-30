"""Best-effort TextGrid label reconciliation for automatic karaoke only.

TIFA can omit zero-width phones, and an entire word disappears from its texts
 tier. Keep the raw model TextGrid; the repaired copy reuses its acoustic
 intervals, assigning the missing confirmed characters to nearby words.
Manual TextGrid import remains strict and unaffected.
"""

from difflib import SequenceMatcher
from pathlib import Path

from textgrid import TextGrid

from .textgrid_import import chars


def reconcile_lyrics(
    grid_path: Path, pronounced_lines: list[str], output: Path
) -> dict:
    """Write a label-only repaired TextGrid preserving model acoustic bounds.

    The input and output must be new files in one attempt directory. Every
    correction is reported. Never silently rewrite the raw TIFA result.
    """
    grid = TextGrid.fromFile(str(grid_path))
    tier = next((tier for tier in grid if tier.name == "texts"), None)
    if tier is None:
        raise ValueError("TIFA TextGrid 缺少 texts tier")
    timed = [entry for entry in tier if str(entry.mark).strip()]
    expected = "".join(chars(line)[0] for line in pronounced_lines)
    actual = "".join(chars(str(entry.mark))[0] for entry in timed)
    if not expected or not actual:
        raise ValueError("TIFA 未提供可恢复的文字时间戳")

    token_for_actual = []
    for i, entry in enumerate(timed):
        token_for_actual.extend([i] * len(chars(str(entry.mark))[0]))
    owners = [None] * len(expected)
    differences = []
    for tag, a, b, c, d in SequenceMatcher(
        None, expected, actual, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            for i in range(b - a):
                owners[a + i] = token_for_actual[c + i]
        else:
            differences.append(
                {
                    "operation": tag,
                    "expected_start": a,
                    "expected": expected[a:b],
                    "observed": actual[c:d],
                }
            )
            if tag == "replace" and d > c:
                for i in range(a, b):
                    pos = c + min(d - c - 1, (i - a) * (d - c) // (b - a))
                    owners[i] = token_for_actual[pos]

    # Fill dropped characters into an observed word in the same original lyric
    # line. If a whole line was dropped, attach to the nearest available
    # anchor; the final strict converter still validates whether it can render.
    boundaries = []
    left = 0
    for line in pronounced_lines:
        right = left + len(chars(line)[0])
        boundaries.append((left, right))
        left = right
    for a, b in boundaries:
        if not any(owner is not None for owner in owners[a:b]):
            # TIFA has no evidence for the entire line; use the nearest
            # preceding token rather than suppressing the whole song.
            before = next(
                (owners[j] for j in range(a - 1, -1, -1) if owners[j] is not None), None
            )
            after = next(
                (owners[j] for j in range(b, len(owners)) if owners[j] is not None),
                None,
            )
            anchor = before if before is not None else after
            if anchor is None:
                raise ValueError("TIFA 未提供有效的歌词锚点")
            for j in range(a, b):
                owners[j] = anchor
            continue
        for j in range(a, b):
            if owners[j] is not None:
                continue
            before = next(
                (owners[k] for k in range(j - 1, a - 1, -1) if owners[k] is not None),
                None,
            )
            after = next(
                (owners[k] for k in range(j + 1, b) if owners[k] is not None), None
            )
            owners[j] = before if before is not None else after

    repaired = [""] * len(timed)
    for char, owner in zip(expected, owners):
        repaired[owner] += char
    changed = 0
    for entry, label in zip(timed, repaired):
        if str(entry.mark) != label:
            changed += 1
            entry.mark = label
    grid.write(str(output))
    return {
        "method": "local_text_label_reconciliation",
        "expected_characters": len(expected),
        "observed_characters": len(actual),
        "changed_intervals": changed,
        "diff": differences[:40],
        "raw_textgrid": str(grid_path),
        "repaired_textgrid": str(output),
    }
