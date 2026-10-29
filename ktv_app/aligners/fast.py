"""Fast lyric alignment: coarse text-only ASR and a handful of native TIFA blocks.

Deliberately trades validation and word-level timing guarantees for latency.
Runs on original extracted audio; does not load Kim or ASR word-time TIFA.
"""

import json
import math
import subprocess
import time
from difflib import SequenceMatcher
from pathlib import Path

from ..media import crop
from ..model_runtime import probe_device, worker_environment
from ..timeline import Timeline, quality
from .recognition import match_text
from .tifa import TIFAAdapter


def coarse_centers(lines, chunks, duration):
    """Locate rough line centers with contiguous ASR text overlap, not timestamps."""
    anchors = []
    for line in lines:
        source = match_text(line.text)
        scored = []
        for chunk in chunks:
            recognized = match_text(chunk["text"])
            if not recognized or not source:
                continue
            matched = [
                m.size
                for m in SequenceMatcher(
                    None, source, recognized, autojunk=False
                ).get_matching_blocks()
                if m.size >= 2
            ]
            if not matched:
                continue
            score = sum(matched) * (0.4 + 0.6 * min(1.0, max(matched) / 4))
            if score >= 3:
                center = (chunk["core_start"] + chunk["core_end"]) / 2
                scored.append((score, center))
        scored.sort(reverse=True)
        if not scored:
            anchors.append(None)
            continue
        strongest = scored[0][0]
        top = [(s, c) for s, c in scored if s >= strongest * 0.75][:3]
        anchors.append(sum(s * c for s, c in top) / sum(s for s, _ in top))

    known = [i for i, value in enumerate(anchors) if value is not None]
    if not known:
        raise ValueError("ASR 无法匹配任何歌词，Fast Mode 无法确定片段")
    for i, center in enumerate(anchors):
        if center is not None:
            continue
        before = max((k for k in known if k < i), default=None)
        after = min((k for k in known if k > i), default=None)
        if before is None:
            anchors[i] = max(0, anchors[after] - (after - i) * 8)
        elif after is None:
            anchors[i] = min(duration, anchors[before] + (i - before) * 8)
        else:
            anchors[i] = anchors[before] + (anchors[after] - anchors[before]) * (
                i - before
            ) / (after - before)
    # Keep sequential order despite repeated choruses and noisy ASR.
    for i in range(1, len(anchors)):
        anchors[i] = max(anchors[i], anchors[i - 1] + 0.2)
    return [min(duration, max(0.0, v)) for v in anchors]


def plan_blocks(lines, chunks, duration):
    centers = coarse_centers(lines, chunks, duration)
    n = len(lines)
    if n == 1:
        return [(0, 1, 0.0, duration)], centers
    # Long gaps are natural breaks; balance the number of lyrics inside each group.
    gaps = [centers[i + 1] - centers[i] for i in range(n - 1)]
    natural = [i + 1 for i, gap in enumerate(gaps) if gap >= 17]
    boundaries = [0, *natural, n]
    split = [0]
    for a, b in zip(boundaries, boundaries[1:]):
        count = b - a
        parts = max(1, math.ceil(count / 4))
        # Avoid singleton final blocks if possible.
        for k in range(1, parts):
            split.append(a + round(count * k / parts))
        split.append(b)
    split = sorted(set(split))
    # Use overlap in sung-through segments and avoid adding lyrics from other
    # blocks when the coarse ASR detects a long silent break.
    cuts = []
    for i in split[1:-1]:
        gap = centers[i] - centers[i - 1]
        cuts.append(((centers[i] + centers[i - 1]) / 2, gap >= 17))
    result = []
    for j, (a, b) in enumerate(zip(split, split[1:])):
        if j == 0:
            left = 0.0
        else:
            time_cut, quiet = cuts[j - 1]
            left = time_cut + 1 if quiet else time_cut - 6
        if j == len(split) - 2:
            right = min(duration, centers[-1] + 8)
        else:
            time_cut, quiet = cuts[j]
            right = time_cut - 1 if quiet else time_cut + 6
        left, right = max(0.0, left), min(duration, right)
        if right - left < 4:
            raise ValueError("粗识别位置不足以构造有效切片")
        result.append((a, b, round(left, 2), round(right, 2)))
    return result, centers


def recognize(audio, lines, settings, work):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    langs = list(dict.fromkeys(line.language for line in lines))
    runtime = probe_device(settings.locator_python, settings.device)
    request = {
        "audio": str(Path(audio).resolve()),
        "work": str(work.resolve()),
        "asr_model": str(settings.asr_model),
        "language": langs[0] if len(langs) == 1 else None,
        "asr_batch_size": 4,
        "chunk_seconds": 12,
        "overlap_seconds": 2,
        "verification_pass": True,
        "cpu_threads": getattr(settings, "cpu_threads", 8),
        **runtime,
    }
    request_path = work / "request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    with (work / "infer.log").open("w") as log:
        run = subprocess.run(
            [
                str(settings.locator_python),
                str(Path(__file__).with_name("locator_worker.py")),
                str(request_path),
            ],
            env=worker_environment(runtime["device"]),
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    if run.returncode:
        raise RuntimeError("Fast Mode ASR 失败，详见 infer.log")
    return json.loads((work / "recognition.asr.json").read_text())


def align_fast(audio, lines, duration, origin, work, settings, event=lambda _: None):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def remaining():
        seconds = 160 - (time.monotonic() - started)
        if seconds <= 0:
            raise TimeoutError("Fast Mode 对齐超过 160 秒预算")
        return seconds

    event("Fast Mode：原音频 Qwen batch 4 粗定位（不分离人声）")
    recognized = recognize(audio, lines, settings, work / "asr")
    event("Fast Mode：按粗位置划分少量 TIFA 片段")
    blocks, centers = plan_blocks(lines, recognized["chunks"], duration)
    (work / "block_plan.json").write_text(
        json.dumps(
            {
                "centers": centers,
                "blocks": blocks,
                "mode": "fast",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    adapter = TIFAAdapter(settings)
    all_lines = []
    errors = []
    with adapter.session(work / "session") as session:
        for index, (a, b, start, end) in enumerate(blocks):
            event(f"Fast Mode：TIFA {index + 1}/{len(blocks)}，歌词 {a + 1}–{b}")
            folder = work / f"block_{index}"
            folder.mkdir()
            clipped = folder / "clip.wav"
            crop(audio, clipped, start, end, settings)
            try:
                timeline = session.align(
                    clipped,
                    lines[a:b],
                    duration,
                    start,
                    folder / "tifa",
                    timeout_s=remaining(),
                )
            except TimeoutError:
                raise
            except (RuntimeError, ValueError, OSError) as error:
                errors.append({"block": index, "reason": str(error)[:300]})
                for line in lines[a:b]:
                    line.units = []
                    line.start = line.end = None
                all_lines.extend(lines[a:b])
            else:
                all_lines.extend(timeline.lines)

        # A large line overlap means adjacent TIFA blocks latched onto the
        # same audio phrase. Retry only those two lines in merged context.
        for index in range(1, len(blocks)):
            a, b, _, right = blocks[index]
            suspicious = [
                i
                for i in range(a, b)
                if not all_lines[i].units
                or any(u.start is None for u in all_lines[i].units)
                or (
                    i > 0
                    and all_lines[i].start is not None
                    and all_lines[i - 1].end is not None
                    and all_lines[i].start < all_lines[i - 1].end - 1
                )
            ]
            if not suspicious:
                continue
            previous_start, _, left, _ = blocks[index - 1]
            if right - left > 110:
                continue
            event(f"Fast Mode：局部修复第 {suspicious[0] + 1} 行的跨段重叠")
            targets = sorted(set([suspicious[0] - 1, *suspicious]))
            for attempt in range(2):
                # Only on an unresolved collision, trim four seconds of
                # preceding context; do not repeatedly rerun other blocks.
                retry_start = left + (4 if attempt else 0)
                if retry_start >= right - 4:
                    break
                folder = work / f"repair_{index}_{attempt}"
                folder.mkdir()
                clipped = folder / "clip.wav"
                crop(audio, clipped, retry_start, right, settings)
                try:
                    candidates = session.align(
                        clipped,
                        [l.model_copy(deep=True) for l in all_lines[previous_start:b]],
                        duration,
                        retry_start,
                        folder / "tifa",
                        timeout_s=remaining(),
                    )
                except TimeoutError:
                    raise
                except (RuntimeError, ValueError, OSError) as error:
                    errors.append({"repair": index, "reason": str(error)[:300]})
                    break
                selected = targets
                if attempt and targets[0] > previous_start:
                    # On the second attempt, also accept the adjacent previous
                    # line if its new start agrees with the initial alignment.
                    # This avoids retaining an overstretched old line end.
                    preceding = targets[0] - 1
                    candidate = candidates.lines[preceding - previous_start]
                    previous = all_lines[preceding]
                    if (
                        candidate.start is not None
                        and previous.start is not None
                        and abs(candidate.start - previous.start) <= 1.5
                    ):
                        selected = [preceding, *targets]
                proposed = [candidates.lines[i - previous_start] for i in selected]
                if any(
                    not candidate.units or any(u.start is None for u in candidate.units)
                    for candidate in proposed
                ):
                    break
                collision = False
                for i, candidate in zip(selected, proposed):
                    predecessor = (
                        proposed[selected.index(i - 1)]
                        if i - 1 in selected
                        else all_lines[i - 1]
                        if i
                        else None
                    )
                    if (
                        predecessor
                        and predecessor.end is not None
                        and candidate.start is not None
                        and candidate.start < predecessor.end - 1
                    ):
                        collision = True
                        break
                if not collision:
                    for i, candidate in zip(selected, proposed):
                        all_lines[i] = candidate
                    break
    timeline = Timeline(
        duration=duration,
        origin=origin,
        lines=all_lines,
        diagnostics={
            "pipeline": "fast_asr_coarse_tifa",
            "asr_elapsed": recognized["asr_elapsed"],
            "blocks": blocks,
            "block_errors": errors,
            "note": "Fast Mode: coarse ASR location, relaxed timing validation",
            "elapsed": round(time.monotonic() - started, 3),
        },
    )
    return quality(timeline)
