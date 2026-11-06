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


def test_long_sustained_final_line_keeps_native_timing_only_at_audio_end():
    # Recorded "为岳" last line: the native TIFA word "过" lasts until 4:14.53.
    # Quarantining its complete 25-second phrase made the estimated fallback
    # incorrectly stop the lyrics at 3:50.44.
    text = "荆水畔 孤身回看 不曾悔过"
    spans = [
        (228.82, 229.01),
        (229.01, 229.28),
        (229.28, 232.92),
        (234.16, 234.57),
        (234.57, 234.82),
        (234.82, 238.43),
        (238.43, 241.62),
        (241.62, 242.05),
        (244.28, 244.92),
        (247.30, 249.53),
        (249.53, 254.53),
    ]
    positions = [i for i, c in enumerate(text) if c != " "]
    ending = Line(
        id="ending",
        text=text,
        units=[
            unit(text[pos], pos, start, end, id=f"end-{i}")
            for i, (pos, (start, end)) in enumerate(zip(positions, spans))
        ],
    )
    previous = Line(
        id="previous",
        text="前一句",
        units=[unit("前一句", 0, 217.15, 224.77, id="prev-u")],
    )
    source = timeline([previous, ending], duration=263)
    assert clean_tifa(source).lines[-1].start is None
    cleaned = clean_tifa(source, terminal_audio=True)
    assert cleaned.lines[-1].start == 228.82
    assert cleaned.lines[-1].end == 254.53
    assert all(u.timing_source == "model" for u in cleaned.lines[-1].units)
    assert not cleaned.diagnostics["quarantined_native"]
    # The exemption is only for the last line of the full audio, not a
    # suspicious long line elsewhere in a block.
    earlier = timeline([ending, previous], duration=263)
    assert clean_tifa(earlier, terminal_audio=True).lines[0].start is None


def test_anchor_pipeline_recognizes_full_audio_terminal_sustain(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf

    import ktv_app.aligners.anchor_policy as policy

    last = Line(
        id="last",
        text="甲乙丙丁戊己庚辛壬癸",
        units=[
            unit(c, i, 5 + i * 2, 7 + i * 2, id=f"last-{i}")
            for i, c in enumerate("甲乙丙丁戊己庚辛壬癸")
        ],
    )
    before = Line(id="before", text="前句", units=[unit("前句", 0, 1, 3)])
    source = timeline([before, last], duration=30)

    class FakeAdapter:
        def __init__(self, settings):
            pass

        def align(self, *args):
            return source.model_copy(deep=True)

        def align_many(self, *args):
            return {}

    audio = tmp_path / "song.wav"
    sf.write(audio, np.zeros(30 * 8000), 8000)
    monkeypatch.setattr(policy, "TIFAAdapter", FakeAdapter)
    output = policy.run_anchor_alignment(
        audio, source.lines, 30, 0, tmp_path / "work", object(), lambda _: None
    )
    assert output.lines[-1].start == 5
    assert output.lines[-1].end == 25
    assert all(u.timing_source == "model" for u in output.lines[-1].units)


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


def _intro_outlier_case(*, language="zh", suffix_gap=False):
    text = "甲乙丙丁戊己庚辛壬癸"
    times = [
        (0.52, 3.48),
        (16.94, 17.31),
        (17.31, 17.60),
        (17.60, 18.29),
        (18.29, 18.65),
        (18.65, 19.05),
        (19.05, 19.53),
        (19.53, 19.90),
        (19.90, 20.16),
        (20.16, 20.86),
    ]
    if suffix_gap:
        times[5] = (8, 9)
    first = Line(
        id="repeat0",
        language=language,
        text=text,
        units=[
            unit(c, i, *span, id=f"first-{i}")
            for i, (c, span) in enumerate(zip(text, times))
        ],
    )
    after = Line(
        id="after",
        text="后续歌词",
        units=[unit("后续歌词", 0, 21.83, 24, id="after-u")],
    )
    return timeline([first, after]), {
        "repeat0": (
            {"start": 16.92, "end": 20.91, "coverage": 0.9, "similarity": 0.9},
            {"start": 16.92, "end": 20.88, "coverage": 0.9, "similarity": 0.9},
        )
    }


def test_two_pass_asr_quarantines_only_spurious_intro_word_not_native_suffix():
    source, evidence = _intro_outlier_case()
    assert all(u.start is None for u in clean_tifa(source).lines[0].units)

    clean = clean_tifa(source, evidence)
    first = clean.lines[0]
    assert first.units[0].start is None
    assert [(u.start, u.end, u.timing_source) for u in first.units[1:]] == [
        (u.start, u.end, u.timing_source) for u in source.lines[0].units[1:]
    ]
    assert clean.diagnostics["quarantined_native"][0]["reason"] == (
        "spurious_intro_word_two_pass_asr"
    )
    assert len(clean.diagnostics["quarantined_native"][0]["units"]) == 1

    completed = complete_timeline(clean, rolling=True)
    assert completed.diagnostics["auto_completion"]["estimated_units"] == 1
    assert completed.diagnostics["auto_completion"]["estimated_lines"] == 1
    assert completed.lines[0].units[0].timing_source == "estimated"
    assert 16.0 < completed.lines[0].start < 16.94
    assert completed.lines[0].end == 20.86
    assert all(u.timing_source == "model" for u in completed.lines[0].units[1:])
    assert completed.lines[1].units[0].start == 21.83
    assert remove_estimates(completed).lines[0].units[1].start == 16.94


def test_introduction_outlier_is_not_salvaged_without_two_reliable_passes():
    source, evidence = _intro_outlier_case()
    left, right = evidence["repeat0"]
    alternatives = [
        None,
        {"repeat0": (left, None)},
        {"repeat0": (left, dict(right, coverage=0.79))},
        {"repeat0": (left, dict(right, similarity=0.79))},
        {"repeat0": (left, dict(right, start=18))},
        {"repeat0": (left, dict(right, end=22))},
        {"repeat0": (dict(left, start=0.52), dict(right, start=0.52))},
    ]
    for candidate in alternatives:
        clean = clean_tifa(source, candidate)
        assert all(u.start is None for u in clean.lines[0].units)
        assert clean.diagnostics["quarantined_native"][0]["reason"] == (
            "within_sentence_conflict_or_long_pause"
        )


def test_spurious_intro_salvage_rejects_japanese_and_multiple_conflicts():
    for options in [{"language": "ja"}, {"suffix_gap": True}]:
        source, evidence = _intro_outlier_case(**options)
        clean = clean_tifa(source, evidence)
        assert all(u.start is None for u in clean.lines[0].units)
        assert not any(
            row["reason"] == "spurious_intro_word_two_pass_asr"
            for row in clean.diagnostics["quarantined_native"]
        )


def test_spurious_intro_salvage_remains_scoped_to_matching_duplicate_line_id():
    source, evidence = _intro_outlier_case()
    duplicated = source.lines[0].model_copy(deep=True)
    duplicated.id = "repeat1"
    duplicated.units = [
        unit(c, i, 40 + i * 0.35, 40 + (i + 1) * 0.35, id=f"repeat1-{i}")
        for i, c in enumerate(duplicated.text)
    ]
    source.lines.append(duplicated)
    clean = clean_tifa(source, evidence)
    assert clean.lines[0].units[0].start is None
    assert clean.lines[-1].units[0].start == 40
    assert len(clean.diagnostics["quarantined_native"]) == 1


def test_anchor_alignment_passes_two_pass_evidence_through_native_and_local_retries(
    tmp_path, monkeypatch
):
    import numpy as np
    import soundfile as sf

    import ktv_app.aligners.anchor_policy as policy

    source, evidence = _intro_outlier_case()

    class FakeAdapter:
        def __init__(self, settings):
            pass

        def align(self, *args):
            return source.model_copy(deep=True)

        def align_many(self, *args):
            return {}

    audio = tmp_path / "vocal.wav"
    sf.write(audio, np.zeros(30 * 8000), 8000)
    monkeypatch.setattr(policy, "TIFAAdapter", FakeAdapter)
    monkeypatch.setattr(policy, "crop", lambda *args: None)
    result = policy.run_anchor_alignment(
        audio,
        source.lines,
        60,
        0,
        tmp_path / "work",
        object(),
        lambda _: None,
        asr_matches=evidence,
    )
    assert result.lines[0].units[0].start is None
    assert result.lines[0].units[1].start == 16.94
    assert result.lines[0].units[-1].end == 20.86
    assert result.lines[1].units[0].start == 21.83
    assert result.diagnostics["asr_used"] is True
    assert result.diagnostics["unit_sources"]["first-1"] == "tifa_global"
    assert "first-0" not in result.diagnostics["unit_sources"]
    assert result.diagnostics["quarantined_native"][0]["reason"] == (
        "spurious_intro_word_two_pass_asr"
    )


def test_spurious_intro_salvage_does_not_trust_stretched_suffix():
    source, evidence = _intro_outlier_case()
    for i, u in enumerate(source.lines[0].units[1:]):
        u.start = 16.94 + i * 3.4
        u.end = u.start + 0.3
    last = source.lines[0].units[-1].end
    evidence["repeat0"][0]["end"] = last
    evidence["repeat0"][1]["end"] = last - 0.05
    clean = clean_tifa(source, evidence)
    assert all(u.start is None for u in clean.lines[0].units)
    assert clean.diagnostics["quarantined_native"][0]["reason"] == (
        "within_sentence_conflict_or_long_pause"
    )
