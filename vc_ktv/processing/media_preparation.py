"""Acquire and render media for song preparation."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from vc_ktv.lyrics.timed_lyrics import offset_timeline
from vc_ktv.pipeline import (
    Line,
    RenderOptions,
    Timeline,
    Unit,
    capped_h264_args,
    make_karaoke_video,
    pipeline_settings,
    probe,
    render,
    write_bundle,
)
from vc_ktv.rendering.karaoke import parse_lrc
from vc_ktv.rendering.subtitle_styles import DEFAULT_PRESET, StylePreset, preset_style
from vc_ktv.sources.bilibili_media import QUALITY_HEIGHTS, public_media_download

log = logging.getLogger(__name__)


def media_download(
    bvid: str, page: int, directory: Path, *, max_height: int = 720
) -> Path:
    if max_height not in QUALITY_HEIGHTS:
        raise ValueError("不支持的清晰度")
    output = directory / "source.%(ext)s"
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-part",
        "--no-progress",
        "--no-warnings",
        "--socket-timeout",
        "12",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
    ]
    # Try higher quality first. If the stream itself fails, retry a lower
    # ceiling, not merely a lower format when a format is unavailable.
    for height in (height for height in QUALITY_HEIGHTS if height <= max_height):
        for old in directory.glob("source.*"):
            old.unlink(missing_ok=True)
        selection = (
            f"bv[height<={height}][ext=mp4]+ba[ext=m4a]/"
            f"bv[height<={height}]+ba/best[height<={height}]"
        )
        result = subprocess.run(
            [
                *args,
                "-f",
                selection,
                "--merge-output-format",
                "mp4",
                "-o",
                str(output),
                f"https://www.bilibili.com/video/{bvid}/?p={page}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            log.warning(
                "yt-dlp <=%sp failed for %s p%s: %s",
                height,
                bvid,
                page,
                (result.stderr[-300:] or result.stdout[-300:]).strip(),
            )
            continue
        matches = [
            p
            for p in directory.glob("source.*")
            if p.suffix in (".mp4", ".webm", ".m4v")
        ]
        if len(matches) == 1 and matches[0].stat().st_size > 0:
            return matches[0]
        log.warning("yt-dlp <=%sp yielded no valid media for %s", height, bvid)
    log.warning(
        "yt-dlp exhausted quality fallbacks for %s p%s; using public API", bvid, page
    )
    return public_media_download(bvid, page, directory, max_height=max_height)


def prepare_tv_source(
    source: Path, archive: Path, duration: float, bandwidth_mbps: float
) -> None:
    """Only transcode high-average-bitrate original video into a private TV cache."""
    if (
        duration <= 0
        or source.stat().st_size * 8 <= duration * bandwidth_mbps * 800_000
    ):
        return
    destination = archive / "tv-source.mp4"
    temporary = archive / "tv-source.tmp.mp4"
    try:
        subprocess.run(
            [
                os.environ.get("KTV_FFMPEG", "ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-map_metadata",
                "0",
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:a",
                "copy",
                *capped_h264_args(stream_video_kbps(bandwidth_mbps)),
                str(temporary),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if temporary.stat().st_size < 1024:
            raise ValueError("invalid compressed source")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def stream_video_kbps(bandwidth_mbps: float) -> int:
    """Reserve 384 kbps for copied audio and MP4 overhead."""
    return max(300, round(bandwidth_mbps * 1000) - 384)


def render_upstream_video(
    source: Path,
    lyrics: str,
    destination: Path,
    archive: Path,
    duration: float,
    *,
    preset: StylePreset = DEFAULT_PRESET,
    max_bandwidth_mbps: float = 3.0,
    progress=None,
) -> dict:
    """Run the selected alignment pipeline; no implicit fallback or retiming."""
    # AutoPrep owns a new attempt directory; the pipeline owns its own
    # isolated child directory and does not mutate the original source.
    options = {
        "style": preset_style(preset),
        "render_options": RenderOptions(
            max_video_kbps=stream_video_kbps(max_bandwidth_mbps),
        ),
    }
    options["mode"] = os.environ.get("VC_KTV_PIPELINE_MODE", "quality")
    if progress is not None:
        options["progress"] = progress
    result = make_karaoke_video(
        source, lyrics, archive / "pipeline", pipeline_settings(), **options
    )
    shutil.copyfile(result.video, destination)
    shutil.copyfile(result.ass.parent / "lyrics.lrc", archive / "aligned.lrc")
    return json.loads(result.report.read_text(encoding="utf-8"))


def render_provider_video(
    source: Path,
    timed_lrc: str,
    offset_ms: int,
    destination: Path,
    archive: Path,
    duration: float,
    *,
    max_bandwidth_mbps: float = 3.0,
    preset: StylePreset = DEFAULT_PRESET,
) -> dict:
    """Render explicitly chosen line timestamps, without loading GPU models."""
    archive.mkdir(exist_ok=True, parents=True)
    shifted = offset_timeline(timed_lrc, offset_ms, duration)
    (archive / "aligned.lrc").write_text(shifted, encoding="utf-8")
    (archive / "provider_timing.json").write_text(
        json.dumps(
            {
                "mode": "provider_line_only",
                "offset_ms": offset_ms,
                "verified_against_audio": False,
                "not_word_aligned": True,
                "character_timing": "equal_split_estimate",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # The provider gives line starts only; synthesize an evenly timed sweep.
    rows = parse_lrc(shifted)
    lines = []
    for i, row in enumerate(rows):
        start = row["start"]
        next_start = rows[i + 1]["start"] if i + 1 < len(rows) else duration
        end = min(next_start, start + 8)
        text = row["text"]
        lines.append(
            Line(
                id=f"provider-{i}",
                text=text,
                start=start,
                end=end,
                completion_note="逐字渐变按来源行时长等分估算，未经过声学验证",
                units=[
                    Unit(
                        id=f"provider-{i}-{j}",
                        text=char,
                        source_start=j,
                        source_end=j + 1,
                        start=start + (end - start) * j / len(text),
                        end=start + (end - start) * (j + 1) / len(text),
                        timing_source="estimated",
                    )
                    for j, char in enumerate(text)
                ],
            )
        )
    bundle = archive / "provider_subtitles"
    write_bundle(
        Timeline(
            duration=duration,
            model="provider_line_only",
            lines=lines,
            diagnostics={
                "timing_source": "provider_lrc",
                "offset_ms": offset_ms,
                "verified_against_audio": False,
                "character_timing": "equal_split_estimate",
            },
        ),
        preset_style(preset),
        bundle,
    )
    options = RenderOptions(
        max_video_kbps=stream_video_kbps(max_bandwidth_mbps),
    )
    # Reuse the same audio-preserving encoder, style/bitrate and container as
    # the full-quality route; only the model alignment stages are skipped.
    video = render(
        source,
        bundle,
        probe(source, pipeline_settings()),
        pipeline_settings(),
        options,
        "karaoke.mp4",
    )
    shutil.copyfile(video, destination)
    return {
        "mode": "provider_line_only",
        "lines": len(rows),
        "offset_ms": offset_ms,
        "verified_against_audio": False,
        "character_timing": "equal_split_estimate",
    }
