"""CPU-only smoke for coarse Fast Mode planning, no GPU model or media."""

import pytest

from ktv_app.aligners.fast import coarse_centers, plan_blocks
from ktv_app.timeline import Line


def chunk(text, left, right):
    return {"text": text, "core_start": left, "core_end": right}


def test_fast_mode_single_line():
    lines = [Line(id="one", text="你好世界")]
    blocks, centers = plan_blocks(lines, [chunk("你好世界", 0, 10)], 60)
    assert blocks == [(0, 1, 0.0, 60)]
    assert centers == [5]


def test_fast_mode_multiple_text_chunks():
    lines = [
        Line(id="a", text="森林迷宫"),
        Line(id="b", text="离开牢笼"),
        Line(id="c", text="眼前荆棘"),
        Line(id="d", text="新的生命"),
    ]
    recognized = [
        chunk("森林迷宫", 0, 12),
        chunk("离开牢笼", 12, 24),
        chunk("眼前荆棘", 24, 36),
        chunk("新的生命", 36, 48),
    ]
    blocks, centers = plan_blocks(lines, recognized, 60)
    assert len(centers) == len(lines)
    assert all(centers[i] < centers[i + 1] for i in range(3))
    assert blocks[0][0] == 0 and blocks[-1][1] == len(lines)
    assert all(block[3] > block[2] for block in blocks)


def test_fast_mode_no_asr_evidence_fails():
    with pytest.raises(ValueError, match="无法匹配"):
        coarse_centers([Line(id="a", text="你好")], [chunk("xyz", 0, 12)], 30)
