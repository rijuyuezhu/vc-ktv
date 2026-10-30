"""Original VC KTV full-song TIFA alignment and strict lyric matching.

Restores pre-upstream pronunciation mapping and TextGrid conversion without ASR,
lyric chunking or fabricated word timings. The caller chooses raw/Kim audio.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from ktv_app.media import run
from ktv_app.timeline import Line, Timeline, Unit

from .karaoke import parse_lrc
from .textgrid_import import convert_textgrid

SCAT_READINGS = {
    "du": "嘟",
    "da": "哒",
    "la": "啦",
    "ha": "哈",
    "aa": "啊",
    "ah": "啊",
    "oh": "哦",
    "na": "呐",
    "ya": "呀",
    "wu": "呜",
    "woo": "呜",
    "ooh": "呜",
}
SCAT_PART = re.compile(
    "|".join(sorted(SCAT_READINGS, key=len, reverse=True)), re.IGNORECASE
)
SCAT_WORD = re.compile(
    r"(?<![A-Za-z])(?:" + SCAT_PART.pattern + r"){2,}(?![A-Za-z])",
    re.IGNORECASE,
)
SCAT_PREFIX = re.compile(
    r"^(?:(?:ha|ah|aa|la|oh|woo|ooh|wu|na|ya)\b[\s~]*){1,8}"
    r"(?=[\u3400-\u9fff])",
    re.IGNORECASE,
)


def pronunciation_lyrics(lyrics: str):
    """Explicit Mandarin pronunciation hints with a source-character mapping.

    Translate only known scat syllables, never ordinary English words. All
    displayed text remains in its original form, including repeated refrains.
    """
    lines = [line.strip() for line in lyrics.splitlines() if line.strip()]
    spoken, maps, changes = [], [], []
    for line_no, display in enumerate(lines):
        replacements = []
        # Repeated scat words anywhere in a line: DUDUDU / DaLaDa.
        for match in SCAT_WORD.finditer(display):
            parts = SCAT_PART.findall(match.group())
            if "".join(parts).casefold() == match.group().casefold():
                replacements.append(
                    (
                        match.start(),
                        match.end(),
                        "".join(SCAT_READINGS[p.lower()] for p in parts),
                    )
                )
        # Older supported case: short independent vocables before Hanzi.
        prefix = SCAT_PREFIX.match(display)
        if prefix:
            for match in re.finditer(r"[A-Za-z]+", prefix.group()):
                if SCAT_PART.fullmatch(match.group()) and not any(
                    start <= match.start() < end for start, end, _ in replacements
                ):
                    replacements.append(
                        (
                            match.start(),
                            match.end(),
                            SCAT_READINGS[match.group().lower()],
                        )
                    )
        replacements.sort()
        cursor, text, spans = 0, [], []
        for begin, end, reading in replacements:
            if begin < cursor:
                continue
            for i in range(cursor, begin):
                text.append(display[i])
                spans.append((i, i + 1))
            for syllable in reading:
                text.append(syllable)
                spans.append((begin, end))
            changes.append(
                {
                    "line": line_no,
                    "source_start": begin,
                    "source_end": end,
                    "display": display[begin:end],
                    "pronunciation": reading,
                }
            )
            cursor = end
        for i in range(cursor, len(display)):
            text.append(display[i])
            spans.append((i, i + 1))
        spoken.append("".join(text))
        maps.append(spans)
    return "\n".join(spoken), maps, changes


def timeline_from_lrc(
    lrc: str, lyrics: str, duration: float, origin: float = 0
) -> Timeline:
    """Adapt strictly validated old LRC to the current subtitle renderer."""
    source = [line.strip() for line in lyrics.splitlines() if line.strip()]
    rows = parse_lrc(lrc)
    if [row["text"] for row in rows] != source:
        raise ValueError("完整歌曲对齐结果与歌词顺序不符")
    lines = []
    warnings = []
    previous_end = 0.0
    for index, (text, row) in enumerate(zip(source, rows)):
        units = []
        cursor = 0
        for j, ((start, value), end) in enumerate(zip(row["units"], row["ends"])):
            if end is None:
                raise ValueError("整首对齐缺少字词结束时间")
            units.append(
                Unit(
                    id=f"full-{index}-{j}",
                    text=value,
                    source_start=cursor,
                    source_end=cursor + len(value),
                    start=start,
                    end=end,
                )
            )
            cursor += len(value)
        if not units or cursor != len(text):
            raise ValueError("整首对齐未覆盖完整歌词")
        if units[0].start < previous_end - 0.02:
            raise ValueError("整首对齐存在跨行时间重叠")
        # Preserve exactly what TIFA generated for the listening trial. Report
        # suspicious durations, but do not reject a complete original TextGrid.
        # These hints are not proof of error (long musical holds are valid).
        span = units[-1].end - units[0].start
        if span < max(0.2, 0.06 * len(units)) or span > max(20, len(units) + 5):
            warnings.append({"line": index + 1, "duration": round(span, 2)})
        previous_end = units[-1].end
        lines.append(
            Line(
                id=f"full-{index}",
                text=text,
                source_line=index,
                start=units[0].start,
                end=units[-1].end,
                units=units,
            )
        )
    for index in range(len(lines) - 3):
        if lines[index + 3].end - lines[index].start < 2:
            warnings.append({"lines": [index + 1, index + 4], "span": "under_2s"})
    return Timeline(
        duration=duration,
        origin=origin,
        lines=lines,
        diagnostics={
            "pipeline": "global_full_song_tifa",
            "estimated_lines": 0,
            "suspicious_timing": warnings,
        },
    )


def align_full_song(
    source: Path, lyrics: str, duration: float, origin: float, work: Path, settings
) -> Timeline:
    """Align one full-song waveform, either original mixed audio or Kim vocals."""
    tifa = Path(settings.tifa_source)
    python = Path(settings.tifa_python)
    model = Path(settings.tifa_model)
    if not all(p.is_file() for p in (python, tifa / "infer.py", model)):
        raise RuntimeError("整首 TIFA 的模型或运行环境未安装")
    work.mkdir(parents=True, exist_ok=True)
    audio = work / "audio.wav"
    run(
        [
            settings.tool("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            str(audio),
        ],
        timeout=180,
        log=work / "extract.log",
    )
    spoken, spans, changes = pronunciation_lyrics(lyrics)
    (work / "lyrics.source.txt").write_text(lyrics, encoding="utf-8")
    (work / "audio.txt").write_text(spoken, encoding="utf-8")
    (work / "pronunciation_trace.json").write_text(
        json.dumps({"substitutions": changes}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out = work / "alignment"
    out.mkdir()
    cmd = [
        str(python),
        str(tifa / "infer.py"),
        str(audio),
        "-m",
        str(model),
        "-l",
        "zh",
        "-o",
        str(out),
        "--batch-size",
        "1",
        "--num-workers",
        "0",
    ]
    with (work / "tifa.log").open("w") as log:
        result = subprocess.run(
            cmd,
            cwd=tifa,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=900,
            env={**os.environ, "OMP_NUM_THREADS": "4"},
        )
    if result.returncode:
        raise RuntimeError("整首 TIFA 推理失败，请查看 tifa.log")
    grid = out / "audio.TextGrid"
    if not grid.is_file():
        raise RuntimeError("整首 TIFA 未生成 TextGrid")
    lrc, report = convert_textgrid(
        grid,
        lyrics,
        duration,
        offset=origin,
        pronunciation=spoken,
        source_spans=spans,
    )
    (work / "aligned.lrc").write_text(lrc, encoding="utf-8")
    (work / "alignment_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return timeline_from_lrc(lrc, lyrics, duration, origin)
