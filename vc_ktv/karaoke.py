"""Render KTV subtitles from user-verified timed LRC (no invented alignment)."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

STAMP = r"(\d{1,3}):([0-5]\d)(?:\.(\d{1,3}))?"
LINE_STAMP = re.compile(rf"^\[({STAMP})\]")
WORD_STAMP = re.compile(rf"<({STAMP})>")


def seconds(groups: tuple[str, ...]) -> float:
    minutes, secs, fraction = groups
    return int(minutes) * 60 + int(secs) + int((fraction or "0").ljust(3, "0")) / 1000


def parse_lrc(text: str) -> list[dict]:
    result = []
    for raw in text.splitlines():
        line = raw.strip()
        starts = []
        while match := LINE_STAMP.match(line):
            starts.append(seconds(match.groups()[1:]))
            line = line[match.end() :]
        if not starts or not line.strip():
            continue
        markers = list(WORD_STAMP.finditer(line))
        if markers:
            if line[: markers[0].start()].strip():
                raise ValueError("逐字时间戳前存在没有时间信息的文字")
            units = []
            ends = []
            for i, marker in enumerate(markers):
                right = markers[i + 1].start() if i + 1 < len(markers) else len(line)
                chunk = line[marker.end() : right]
                when = seconds(marker.groups()[1:])
                if chunk:
                    units.append((when, chunk))
                    ends.append(None)
                elif units:
                    if ends[-1] is not None:
                        raise ValueError("重复的逐字结束时间戳")
                    ends[-1] = when
                else:
                    raise ValueError("逐字开始时间之前出现无内容的时间标记")
            if not units:
                continue
            if any(b[0] <= a[0] for a, b in zip(units, units[1:])):
                raise ValueError("逐字时间戳必须严格递增")
            for i, ((when, _), end) in enumerate(zip(units, ends)):
                next_start = units[i + 1][0] if i + 1 < len(units) else None
                if end is not None and (
                    end <= when or (next_start is not None and end > next_start)
                ):
                    raise ValueError("逐字结束时间戳越界或与下一字重叠")
            # Enhanced LRC is tied to an absolute recording time, not copied to repeated lines.
            if len(starts) != 1:
                raise ValueError("逐字 LRC 不支持一行多个复用时间标签")
            result.append(
                {
                    "start": starts[0],
                    "text": "".join(x[1] for x in units),
                    "units": units,
                    "ends": ends,
                }
            )
        else:
            result.extend(
                {"start": start, "text": line, "units": [], "ends": []}
                for start in starts
            )
    result.sort(key=lambda line: line["start"])
    if not result:
        raise ValueError("没有找到带 [mm:ss.xx] 的歌词，请先导入时间轴")
    return result


def escape_ass(text: str) -> str:
    return (
        text.replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\n", " ")
    )


def ass_time(t: float) -> str:
    n = max(0, round(t * 100))
    return f"{n // 360000}:{n // 6000 % 60:02d}:{n // 100 % 60:02d}.{n % 100:02d}"


def write_ass(lyrics: str, dest: Path, duration: float, lead: float = 1.8) -> dict:
    if not 0 <= lead <= 5:
        raise ValueError("提前量须为 0–5 秒")
    lines = parse_lrc(lyrics)
    if lines[-1]["start"] >= duration:
        raise ValueError("歌词起点超过视频时长")
    if any(line["start"] >= duration for line in lines):
        raise ValueError("歌词包含超出视频范围的时间戳")
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: KTV,Noto Sans CJK SC,62,&H0000D5FF,&H00FFFFFF,&H00202020,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,50,50,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    timed_units = 0
    for i, line in enumerate(lines):
        start = line["start"]
        next_start = lines[i + 1]["start"] if i + 1 < len(lines) else duration
        if next_start <= start:
            raise ValueError("逐句时间戳重复或重叠")
        end = min(duration, start + min(8, max(1, next_start - start)))
        if line.get("ends") and line["ends"][-1] is not None:
            end = line["ends"][-1]
            if end > duration + 0.02 or end > next_start:
                raise ValueError("句尾时间越界或与下一句重叠")
        display_at = max(0, start - lead)
        if line["units"]:
            # Never shift highlighting when changing the advance-display amount.
            parts = []
            cursor = display_at
            for u, (when, value) in enumerate(line["units"]):
                if when < start - 0.05 or when >= end:
                    raise ValueError("逐字时间戳与句子区间不一致")
                if when > cursor:
                    parts.append(
                        r"{\k" + str(round((when - cursor) * 100)) + "}" + "\u200b"
                    )
                following = (
                    line["ends"][u]
                    if line["ends"][u] is not None
                    else (
                        line["units"][u + 1][0] if u + 1 < len(line["units"]) else end
                    )
                )
                if following <= when:
                    raise ValueError("逐字时间倒退")
                parts.append(
                    r"{\kf"
                    + str(max(1, round((following - when) * 100)))
                    + "}"
                    + escape_ass(value)
                )
                cursor = following
                timed_units += 1
            body = "".join(parts)
        else:
            # No word-level timing is available. Show the line plainly; do not fake a sweep.
            body = r"{\1c&H00FFFFFF&}" + escape_ass(line["text"])
        y = 900 if i % 2 == 0 else 980
        content = rf"{{\an2\pos(960,{y})}}" + body
        events.append(
            f"Dialogue: 0,{ass_time(display_at)},{ass_time(end)},KTV,,0,0,0,,{content}"
        )
    dest.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
    return {
        "lines": len(lines),
        "timed_units": timed_units,
        "line_only": len([l for l in lines if not l["units"]]),
        "lead": lead,
    }


def render_video(
    source: Path, lyrics: str, target: Path, duration: float, lead: float
) -> dict:
    # ffmpeg executes inside the ASS temp directory; external media paths must be absolute.
    source, target = source.resolve(), target.resolve()
    with tempfile.TemporaryDirectory(prefix="vc-ktv-render-") as directory:
        folder = Path(directory)
        report = write_ass(lyrics, folder / "lyrics.ass", duration, lead)
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            "ass=lyrics.ass",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(target),
        ]
        subprocess.run(cmd, cwd=folder, check=True, capture_output=True, timeout=1200)
        if not target.is_file() or target.stat().st_size < 1024:
            raise RuntimeError("字幕视频未生成")
        return report
