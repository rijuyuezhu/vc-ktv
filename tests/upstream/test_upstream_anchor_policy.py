from ktv_app.aligners.anchor_policy import (
    POLICY,
    build_parts,
    clean_tifa,
    endpoints,
)
from ktv_app.completion import complete_timeline, remove_estimates
from ktv_app.subtitles import display_windows
from ktv_app.timeline import Line, Style, Timeline, Unit, quality


def unit(text, lo, a=None, b=None, id=None):
    return Unit(
        id=id or f"u{lo}",
        text=text,
        source_start=lo,
        source_end=lo + len(text),
        start=a,
        end=b,
    )


def timeline(lines, duration=60):
    return quality(
        Timeline(
            duration=duration, lines=lines, diagnostics={"boundary_policy": POLICY}
        )
    )


def test_missing_first_word_merges_previous_instead_of_using_middle_start():
    a = Line(id="a", text="前句", units=[unit("前句", 0, 10, 12, id="a-u")])
    b = Line(
        id="b",
        text="甲乙丙",
        units=[unit("甲", 0, id="b0"), unit("乙丙", 1, 15, 17, id="b1")],
    )
    t = timeline([a, b])
    assert endpoints(t.lines[1]) == (None, 17)
    parts, reasons = build_parts(t)
    assert (
        parts[0]["indices"] == [0, 1]
        and parts[0]["start"] == 10
        and parts[0]["end"] == 17
    )
    assert reasons[0]["reason"] == "missing_first_word"


def test_missing_last_word_merges_next_and_keeps_original_line_ids():
    a = Line(
        id="a",
        text="甲乙",
        units=[unit("甲", 0, 10, 11, id="a0"), unit("乙", 1, id="a1")],
    )
    b = Line(id="b", text="后句", units=[unit("后句", 0, 15, 17, id="b0")])
    parts, _ = build_parts(timeline([a, b]))
    assert parts[0]["line_ids"] == ["a", "b"] and parts[0]["end"] == 17


def test_entire_missing_sentence_transitively_merges_both_neighbours():
    lines = [
        Line(id="a", text="前", units=[unit("前", 0, 10, 11, id="a0")]),
        Line(id="b", text="缺句"),
        Line(id="c", text="后", units=[unit("后", 0, 20, 21, id="c0")]),
    ]
    t = clean_tifa(timeline(lines))
    parts, _ = build_parts(t)
    assert parts[0]["indices"] == [0, 1, 2]


def test_one_missing_word_uses_exact_neighbour_gap_without_rebuilding_native_words():
    line = Line(
        id="l",
        text="甲乙丙",
        units=[unit("甲", 0, 10, 11), unit("乙", 1), unit("丙", 2, 13, 14)],
    )
    out = complete_timeline(timeline([line]), rolling=True)
    assert [(u.start, u.end, u.timing_source) for u in out.lines[0].units] == [
        (10, 11, "model"),
        (11, 13, "estimated"),
        (13, 14, "model"),
    ]
    assert remove_estimates(out).lines[0].units[0].start == 10


def test_continuous_missing_words_use_character_weights_between_fixed_anchors():
    l = Line(
        id="l",
        text="甲乙乙丙丁丁丁戊",
        units=[
            unit("甲", 0, 10, 11),
            unit("乙乙", 1),
            unit("丙", 3),
            unit("丁丁丁", 4),
            unit("戊", 7, 14, 15),
        ],
    )
    out = complete_timeline(timeline([l]), rolling=True)
    assert [(u.start, u.end) for u in out.lines[0].units] == [
        (10, 11),
        (11, 12),
        (12, 12.5),
        (12.5, 14),
        (14, 15),
    ]


def test_tight_single_missing_word_does_not_erase_entire_tifa_line():
    l = Line(
        id="l",
        text="甲乙丙",
        units=[unit("甲", 0, 10, 11), unit("乙", 1), unit("丙", 2, 11.01, 12)],
    )
    out = complete_timeline(timeline([l]), rolling=True)
    assert out.lines[0].units[0].timing_source == "model"
    assert out.lines[0].units[-1].start == 11.01
    assert out.lines[0].units[1].start == 11 and out.lines[0].units[1].end == 11.01


def test_captured_collapsed_lala_is_quarantined_before_cutting_or_completion():
    a = Line(id="a", text="前句", units=[unit("前句", 0, 280, 282, id="a0")])
    b = Line(
        id="b",
        text="啦儿啦",
        units=[
            unit("啦", 0, 284.48, 284.50),
            unit("儿", 1, 284.50, 284.52),
            unit("啦", 2, 284.52, 284.53),
        ],
    )
    c = Line(id="c", text="后句", units=[unit("后句", 0, 288, 290, id="c0")])
    clean = clean_tifa(timeline([a, b, c], 300))
    assert all(u.start is None for u in clean.lines[1].units)
    assert clean.diagnostics["quarantined_native"][0]["reason"] == "collapsed_sentence"
    out = complete_timeline(clean, rolling=True)
    assert out.lines[1].start == 282 and out.lines[1].end == 288
    assert out.lines[0].units[0].start == 280 and out.lines[2].units[0].start == 288


def test_long_instrumental_hides_completed_line_and_delays_upcoming_line():
    lines = [
        Line(id=str(i), text="歌词", units=[unit("歌词", 0, a, a + 2, id=f"{i}-u")])
        for i, a in enumerate([10, 13, 40, 43])
    ]
    t = timeline(lines)
    w = display_windows(t, Style())
    assert w["1"]["end"] <= 15.5
    assert w["2"]["start"] == 38
    assert not [x for x in w.values() if x["start"] <= 30 < x["end"]]


def test_zero_space_missing_sentence_is_not_forced_into_millisecond_sweeps():
    lines = [
        Line(id="a", text="前句", units=[unit("前句", 0, 10, 12, id="a0")]),
        Line(id="b", text="需要补齐"),
        Line(id="c", text="后句", units=[unit("后句", 0, 12, 15, id="c0")]),
    ]
    out = complete_timeline(timeline(lines), rolling=True)
    assert out.lines[1].end - out.lines[1].start >= 1
    assert out.lines[0].units[0].end == 12
    assert out.diagnostics["auto_completion"]["released_conflicting_anchors"]


def test_two_pass_asr_hint_delays_unanchored_post_interlude_sentence():
    lines = [
        Line(
            id="before", text="前一句", units=[unit("前一句", 0, 8, 11, id="before-u")]
        ),
        Line(id="missing", text="间奏后第一句"),
        Line(
            id="after", text="后一句", units=[unit("后一句", 0, 46, 49, id="after-u")]
        ),
    ]
    source = timeline(lines)
    without_hint = complete_timeline(source, rolling=True)
    assert without_hint.lines[1].start == 11
    assert without_hint.lines[1].end < 20
    source.diagnostics["asr_line_hints"] = {
        "missing": {"start": 39, "end": 43, "coverage": 0.7, "similarity": 0.8}
    }
    result = complete_timeline(source, rolling=True)
    assert result.lines[1].start == 39 and result.lines[1].end == 43
    assert all(u.timing_source == "estimated" for u in result.lines[1].units)
    assert result.lines[0].units[0].start == 8
    assert result.lines[2].units[0].start == 46
    assert result.diagnostics["auto_completion"]["changes"][0]["reason"] == (
        "two_pass_asr_bounded_estimate"
    )
    windows = display_windows(result, Style())
    assert windows["missing"]["start"] >= 37
    assert not any(w["start"] <= 30 < w["end"] for w in windows.values())
    assert not remove_estimates(result).lines[1].units


def test_asr_hints_do_not_change_short_gaps_or_partly_anchored_lines():
    short = timeline(
        [
            Line(id="a", text="甲", units=[unit("甲", 0, 10, 11, id="a1")]),
            Line(id="b", text="乙丙"),
            Line(id="c", text="丁", units=[unit("丁", 0, 14, 15, id="c1")]),
        ]
    )
    short.diagnostics["asr_line_hints"] = {"b": {"start": 12, "end": 13}}
    result = complete_timeline(short, rolling=True)
    assert result.lines[1].start == 11 and result.lines[1].end == 14
    mixed = timeline(
        [
            Line(id="a", text="甲", units=[unit("甲", 0, 10, 11, id="a2")]),
            Line(id="b", text="乙丙", units=[unit("乙", 0), unit("丙", 1, 42, 43)]),
            Line(id="c", text="丁", units=[unit("丁", 0, 46, 47, id="c2")]),
        ]
    )
    mixed.diagnostics["asr_line_hints"] = {"b": {"start": 40, "end": 42}}
    result = complete_timeline(mixed, rolling=True)
    assert result.lines[1].units[0].start < 20
    assert result.lines[1].units[1].start == 42


def test_block_completion_receives_only_source_scoped_asr_hints():
    base = timeline(
        [
            Line(id="a", text="甲", units=[unit("甲", 0, 8, 11, id="aa")]),
            Line(id="b", text="缺失句"),
            Line(id="c", text="丁", units=[unit("丁", 0, 46, 49, id="cc")]),
        ]
    )
    base.diagnostics.update(
        {
            "alignment_blocks": [
                {
                    "id": "0",
                    "line_ids": ["a", "b", "c"],
                    "effective_audio_bounds": [0, 60],
                }
            ],
            "asr_line_hints": {
                "b": {"start": 38, "end": 42},
                "other": {"start": 2, "end": 4},
            },
        }
    )
    result = complete_timeline(base, rolling=True)
    assert result.lines[1].start == 38
    assert result.lines[1].end == 42
    assert (
        result.diagnostics["auto_completion"]["changes"][0]["reason"]
        == "two_pass_asr_bounded_estimate"
    )
