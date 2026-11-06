"""TIFA endpoints define parts; retry locally before explicit display estimates."""

import json
import statistics
import subprocess
from pathlib import Path

from ..media import crop
from ..timeline import Timeline, canonical, quality
from .tifa import TIFAAdapter

POLICY = "tifa_endpoints_only_v3"


def text_positions(line):
    return [i for i, c in enumerate(line.text) if canonical(c)]


def endpoints(line):
    """Actual first/last source words, never first/last successfully timed words."""
    positions = text_positions(line)
    if not positions:
        return None, None
    first = next(
        (u for u in line.units if u.source_start <= positions[0] < u.source_end), None
    )
    last = next(
        (u for u in line.units if u.source_start <= positions[-1] < u.source_end), None
    )
    return (first.start if first else None), (last.end if last else None)


def scaffold(line):
    # Retain TIFA semantic units, including its explicitly missing words.
    from ..completion import _make_units

    _make_units(line)
    return line


def clear(unit):
    unit.start = unit.end = None
    unit.phones = []
    unit.readings = []


def lyric_rate(timeline):
    values = []
    for line in timeline.lines:
        a, b = endpoints(line)
        n = len(canonical(line.text))
        if a is not None and b is not None and n and 0.08 <= (b - a) / n <= 0.9:
            values.append((b - a) / n)
    return max(0.08, min(0.6, statistics.median(values) if values else 0.25))


def _spurious_intro_word(line, native, matches, previous):
    """Two independent ASR passes may disprove one early TIFA word, not time the rest."""
    if (
        line.language != "zh"
        or len(native) < 5
        or len(native) != len(line.units)
        or any(u.timing_source != "model" for u in native)
        or native[0].source_start != 0
        or native[0].source_end != native[1].source_start
        or native[-1].source_end != len(line.text)
        or native[0].end - native[0].start < 1
        or native[1].start - native[0].end < 8
        or native[-1].end - native[1].start
        > max(12, (len(canonical(line.text)) - 1) * 1.1 + 3)
        or (previous is not None and native[1].start < previous - 0.001)
        or any(
            b.start < a.end - 0.001 or b.start - a.end > 4
            for a, b in zip(native[1:], native[2:])
        )
        or any(u.end - u.start > 8 for u in native[1:])
    ):
        return False
    left, right = matches or (None, None)
    if not left or not right:
        return False
    if any(
        m.get("coverage", 0) < 0.8 or m.get("similarity", 0) < 0.8
        for m in (left, right)
    ):
        return False
    return (
        abs(left["start"] - right["start"]) <= 0.5
        and abs(left["end"] - right["end"]) <= 0.5
        and all(
            abs(m["start"] - native[1].start) <= 0.5
            and abs(m["end"] - native[-1].end) <= 0.5
            for m in (left, right)
        )
    )


def clean_tifa(timeline, asr_matches=None, *, terminal_audio=False):
    """Quarantine impossible spans while preserving valid terminal sustains."""
    result = timeline.model_copy(deep=True)
    rate = lyric_rate(result)
    rejected = []
    previous = None
    for i, line in enumerate(result.lines):
        scaffold(line)
        native = [u for u in line.units if u.start is not None]
        evidence = (asr_matches or {}).get(line.id)
        if _spurious_intro_word(line, native, evidence, previous):
            rejected.append(
                {
                    "line_id": line.id,
                    "text": line.text,
                    "reason": "spurious_intro_word_two_pass_asr",
                    "units": [native[0].model_dump()],
                }
            )
            clear(native[0])
            native = native[1:]
        a, b = endpoints(line)
        chars = max(1, len(canonical(line.text)))
        reason = None
        if a is not None and b is not None:
            if b - a < max(0.3 if chars >= 3 else 0.16, chars * max(0.06, rate * 0.25)):
                reason = "collapsed_sentence"
            elif b - a > max(12, chars * 1.1 + 3) and not (
                terminal_audio
                and i == len(result.lines) - 1
                and len(native) == len(line.units)
            ):
                # A fully timed final refrain may sustain its last words for
                # much longer than conversational speech. Per-word gap and
                # duration checks below still quarantine impossible timings.
                reason = "stretched_sentence"
        if any(u.end - u.start > 8 for u in native):
            reason = "stretched_word"
        if any(
            v.start < u.end - 0.001 or v.start - u.end > 4
            for u, v in zip(native, native[1:])
        ):
            reason = "within_sentence_conflict_or_long_pause"
        if previous is not None and native and native[0].start < previous - 0.001:
            reason = "sentence_order_conflict"
        if reason:
            rejected.append(
                {
                    "line_id": line.id,
                    "text": line.text,
                    "reason": reason,
                    "units": [u.model_dump() for u in native],
                }
            )
            for u in native:
                if u.timing_source != "manual":
                    clear(u)
        else:
            if native:
                previous = native[-1].end
    result = quality(result)
    result.diagnostics["quarantined_native"] = rejected
    return result


def missing_runs(timeline):
    flat = [(li, u) for li, l in enumerate(timeline.lines) for u in l.units]
    runs = []
    start = None
    for i, (_, u) in enumerate(flat + [(None, None)]):
        if u is not None and u.start is None:
            if start is None:
                start = i
        elif start is not None:
            runs.append(
                {
                    "items": flat[start:i],
                    "before": flat[start - 1][1] if start else None,
                    "after": u,
                    "flat_start": start,
                    "flat_end": i,
                }
            )
            start = None
    return runs


def build_parts(timeline, lower=None, upper=None):
    """Union incomplete endpoints, keeping every original lyric line and ID."""
    lines = timeline.lines
    n = len(lines)
    lower = timeline.origin if lower is None else lower
    upper = timeline.duration if upper is None else upper
    parent = list(range(n))
    merges = []

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b, reason):
        if root(a) != root(b):
            parent[root(b)] = root(a)
            merges.append({"left": lines[a].id, "right": lines[b].id, "reason": reason})

    for i, line in enumerate(lines):
        a, b = endpoints(line)
        if a is None and n > 1:
            union(max(0, i - 1), i if i else 1, "missing_first_word")
        if b is None and n > 1:
            union(i if i < n - 1 else i - 1, min(n - 1, i + 1), "missing_last_word")
        if i and a is not None:
            _, prev = endpoints(lines[i - 1])
            if prev is not None and a < prev - 0.001:
                union(i - 1, i, "overlapping_endpoints")
    groups = {}
    for i in range(n):
        groups.setdefault(root(i), []).append(i)
    parts = []
    for indices in groups.values():
        a, _ = endpoints(lines[indices[0]])
        _, b = endpoints(lines[indices[-1]])
        natives = [u for i in indices for u in lines[i].units if u.start is not None]
        before = [
            u.end for l in lines[: indices[0]] for u in l.units if u.start is not None
        ]
        after = [
            u.start
            for l in lines[indices[-1] + 1 :]
            for u in l.units
            if u.start is not None
        ]
        a = (max(before) if before else lower) if a is None else a
        b = (min(after) if after else upper) if b is None else b
        if b <= a:
            a = min((u.start for u in natives), default=lower)
            b = max((u.end for u in natives), default=upper)
        parts.append(
            {
                "indices": indices,
                "line_ids": [lines[i].id for i in indices],
                "start": a,
                "end": b,
                "merged": len(indices) > 1,
                "needs_retry": any(
                    u.start is None for i in indices for u in lines[i].units
                ),
            }
        )
    return parts, merges


def merge_native(base, local, lower, upper):
    """Local TIFA fills missing source words; accepted global anchors stay fixed."""
    result = base.model_copy(deep=True)
    sources = result.diagnostics.setdefault("unit_sources", {})
    for i, line in enumerate(result.lines):
        candidates = local.lines[i].units
        for candidate in candidates:
            if (
                candidate.start is None
                or not lower <= candidate.start < candidate.end <= upper
            ):
                continue
            covered = [
                u
                for u in line.units
                if max(u.source_start, candidate.source_start)
                < min(u.source_end, candidate.source_end)
            ]
            if not covered or any(u.start is not None for u in covered):
                continue
            if (
                covered[0].source_start != candidate.source_start
                or covered[-1].source_end != candidate.source_end
            ):
                continue
            before = [
                u for l in result.lines[:i] for u in l.units if u.start is not None
            ]
            before += [
                u
                for u in line.units
                if u.start is not None and u.source_end <= candidate.source_start
            ]
            after = [
                u
                for u in line.units
                if u.start is not None and u.source_start >= candidate.source_end
            ]
            after += [
                u for l in result.lines[i + 1 :] for u in l.units if u.start is not None
            ]
            if before and candidate.start < before[-1].end - 0.001:
                continue
            if after and candidate.end > after[0].start + 0.001:
                continue
            line.units = [u for u in line.units if u not in covered] + [
                candidate.model_copy(deep=True)
            ]
            line.units.sort(key=lambda u: u.source_start)
            sources[candidate.id] = "tifa_local"
    return quality(result)


def run_anchor_alignment(
    audio,
    lines,
    duration,
    origin,
    work,
    settings,
    event,
    reuse_work=None,
    tifa_session=None,
    asr_matches=None,
):
    """A new run uses native TIFA first; no Qwen coarse path or mixed global/local words."""
    import soundfile as sf

    from .common import blank

    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    limit = min(duration, origin + sf.info(audio).duration)
    event("第一阶段：TIFA 全曲对齐，按原歌词首词与尾词确定边界")
    try:
        # Normal application runs always perform actual inference.
        adapter = tifa_session or TIFAAdapter(settings)
        raw = adapter.align(audio, blank(lines), duration, origin, work / "global_tifa")
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        raw = Timeline(
            duration=duration,
            origin=origin,
            lines=blank(lines),
            diagnostics={"warnings": [str(error)[:800]]},
        )
    (work / "global_tifa.json").write_text(
        raw.model_dump_json(indent=2), encoding="utf-8"
    )
    terminal_audio = limit >= duration - 0.05
    output = clean_tifa(raw, asr_matches, terminal_audio=terminal_audio)
    output.diagnostics.update(
        boundary_policy=POLICY,
        pipeline="tifa_endpoint_parts_only",
        primary_sentence_model="tifa",
        primary_unit_model="tifa",
        asr_used=any(
            row["reason"] == "spurious_intro_word_two_pass_asr"
            for row in output.diagnostics["quarantined_native"]
        ),
        global_runs={"tifa": raw.diagnostics},
        unit_sources={
            u.id: "tifa_global"
            for l in output.lines
            for u in l.units
            if u.start is not None
        },
        original_native_lines={l.id: l.model_dump() for l in raw.lines},
    )
    parts, merges = build_parts(output, origin, limit)
    output.diagnostics["sentence_parts"] = parts
    output.diagnostics["sentence_merges"] = merges
    jobs = []
    for pi, part in enumerate(parts):
        if not part["needs_retry"]:
            continue
        indices = part["indices"]
        a = max(origin, part["start"] - 0.6)
        b = min(limit, part["end"] + 0.6)
        if b <= a or b - a > 180:
            output.diagnostics.setdefault("warnings", []).append(
                f"合并块 {pi} 范围过宽或冲突，保留补齐诊断"
            )
            continue
        folder = work / f"part_{pi:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        crop(audio, folder / "audio.wav", a - origin, b - origin, settings)
        jobs.append(
            dict(
                id=str(pi),
                audio=folder / "audio.wav",
                lines=blank([lines[i] for i in indices]),
                origin=a,
                end=b,
                folder=folder,
                indices=indices,
            )
        )
    event(f"第二阶段：TIFA 重试 {len(jobs)} 个合并或异常块，已确认的原生时间保持固定")
    try:
        # Local retries are independent batch jobs. They still use the
        # existing batch adapter; primary blocks share a session.
        recovered = TIFAAdapter(settings).align_many(
            jobs, duration, work / "part_tifa_batch"
        )
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        recovered = {}
        output.diagnostics.setdefault("warnings", []).append(str(error)[:800])
    output.diagnostics["local_runs"] = []
    for job in jobs:
        candidate = recovered.get(job["id"])
        if candidate is None:
            continue
        candidate = clean_tifa(
            candidate,
            asr_matches,
            terminal_audio=terminal_audio and job["indices"][-1] == len(lines) - 1,
        )
        sub = Timeline(
            duration=duration,
            origin=origin,
            lines=[output.lines[i] for i in job["indices"]],
            diagnostics={"unit_sources": output.diagnostics["unit_sources"]},
        )
        sub = merge_native(sub, candidate, job["origin"], job["end"])
        for i, l in zip(job["indices"], sub.lines):
            output.lines[i] = l
        output.diagnostics["unit_sources"].update(sub.diagnostics["unit_sources"])
        output.diagnostics["local_runs"].append(
            {
                "id": job["id"],
                "start": job["origin"],
                "end": job["end"],
                "line_ids": [l.id for l in candidate.lines],
                "tifa": candidate.diagnostics,
            }
        )
    output = quality(output)
    output.diagnostics["effective_audio_bounds"] = [origin, limit]
    output.diagnostics["note"] = (
        "TIFA 首尾词定块；原生时间固定；局部 TIFA 重试后剩余缺口按字符数估算。"
    )
    (work / "fusion_plan.json").write_text(
        json.dumps(output.diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return quality(output)
