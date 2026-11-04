"""Optional real CUDA quality-pipeline smoke using only synthesized test media.

Requires espeak-ng, FFmpeg/libx264/libass, and existing Kim/Qwen/TIFA assets.
Does not start a KTV event or read live data. Not suitable for ordinary CI.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path

from vc_ktv.upstream_pipeline import make_karaoke_video, spark_settings

LYRICS = "你好世界\n今天我们一起唱歌\n谢谢大家\n"
SPEECH = "你好世界。今天我们一起唱歌。谢谢大家。"


def command(args: list[str], *, binary=False):
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=not binary,
        timeout=40,
    ).stdout


def generate_media(root: Path) -> Path:
    voice = root / "voice.wav"
    source = root / "speech.mp4"
    command(["espeak-ng", "-v", "zh", "-s", "115", "-w", str(voice), SPEECH])
    command(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=320x180:r=15",
            "-i",
            str(voice),
            "-vf",
            "format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(source),
        ]
    )
    return source


def audio_hash(path: Path) -> str:
    stream = command(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-f",
            "adts",
            "-",
        ],
        binary=True,
    )
    return hashlib.sha256(stream).hexdigest()


def verify(result, source: Path) -> dict:
    run = json.loads(result.run_metadata.read_text(encoding="utf-8"))
    report = json.loads(result.report.read_text(encoding="utf-8"))
    timeline = json.loads(result.timeline.read_text(encoding="utf-8"))
    assert run["status"] == "done", run
    assert run["alignment_quality"] == "model", run
    assert not run.get("fallback_attempts"), run
    assert timeline["diagnostics"]["pipeline"] == "verified_asr_blocks_tifa"
    assert report["total_lines"] == report["rendered_lines"] == 3, report
    assert report["timed_units"] > 0, report
    assert report["estimated_lines"] == report["unmatched_lines"] == 0, report
    assert result.video.is_file() and result.video.stat().st_size > 0
    assert result.ass.is_file() and result.ass.stat().st_size > 0
    assert (result.ass.parent / "lyrics.lrc").read_text(encoding="utf-8").strip()

    tracks = json.loads(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,codec_type,width,height",
                "-of",
                "json",
                str(result.video),
            ]
        )
    )["streams"]
    assert any(
        track["codec_type"] == "video"
        and track["codec_name"] == "h264"
        and track["width"] == 320
        and track["height"] == 180
        for track in tracks
    ), tracks
    assert any(
        track["codec_type"] == "audio" and track["codec_name"] == "aac"
        for track in tracks
    ), tracks
    assert audio_hash(source) == audio_hash(result.video), "AAC audio was altered"
    return {
        "alignment_quality": run["alignment_quality"],
        "alignment_pipeline": timeline["diagnostics"]["pipeline"],
        "rendered_lines": report["rendered_lines"],
        "timed_units": report["timed_units"],
        "fallback_attempts": run.get("fallback_attempts", []),
        "aac_stream_unchanged": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(
            os.getenv("VC_KTV_PIPELINE_ROOT", "~/.local/share/vc-ktv/pipeline")
        ).expanduser(),
        help="Existing read-only GPU model/runtime root",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="New directory for retained test outputs; default is cleaned on exit",
    )
    args = parser.parse_args()
    with ExitStack() as stack:
        if args.out:
            root = args.out.expanduser().resolve()
            root.mkdir(mode=0o700, parents=True, exist_ok=False)
        else:
            root = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="vcktv-gpu-smoke-")
                )
            )
        source = generate_media(root)
        result = make_karaoke_video(
            source,
            LYRICS,
            root / "quality",
            spark_settings(args.root),
            mode="quality",
            progress=lambda message: print(message, flush=True),
        )
        print(json.dumps(verify(result, source), ensure_ascii=False, indent=2))
        print("PASS real Kim/Qwen/TIFA quality alignment and unchanged AAC")


if __name__ == "__main__":
    main()
