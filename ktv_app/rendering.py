import logging
import re
import subprocess
import unicodedata
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .media import run

log = logging.getLogger(__name__)
MAX_KARAOKE_BYTES = 30_000_000
# Reserve room for bitrate variation, subtitles, audio/mux overhead.
COMPACT_TARGET_BYTES = 28_000_000


class RenderOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int | None = Field(default=None, ge=160, le=7680)
    height: int | None = Field(default=None, ge=120, le=4320)
    # Slightly smaller H.264 karaoke MP4; audio remains stream-copied verbatim.
    crf: int = Field(default=23, ge=0, le=40)
    preset: str = Field(default="medium", pattern=r"^(ultrafast|fast|medium|slow)$")
    output_dir: str = ""

    @model_validator(mode="after")
    def even_dimensions(self):
        if bool(self.width) != bool(self.height) or any(
            v is not None and v % 2 for v in (self.width, self.height)
        ):
            raise ValueError("分辨率宽高必须同时提供且为偶数")
        return self


def video_filename(song, artist, video):
    title = str(song or "").strip() or Path(video).stem or "歌曲"
    name = title + (" - " + str(artist).strip() if str(artist or "").strip() else "")
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    name = re.sub(r"\s+", " ", name).strip(" .")[:140].rstrip(" .") or "歌曲"
    return name + "_KTV.mp4"


def render(video, bundle, info, settings, options, output_name=None, *, timeout_s=1800):
    folder = Path(bundle).resolve()
    target = folder / (output_name or video_filename("", "", video))
    if target.parent != folder or target.suffix.lower() != ".mp4":
        raise ValueError("成片文件名无效")
    cmd = [settings.tool("ffmpeg"), "-nostdin", "-v", "warning", "-y", "-i", video]
    filters = ["subtitles=filename='lyrics.ass'"]
    if options.width:
        filters.append(
            f"scale={options.width}:{options.height}:force_original_aspect_ratio=decrease"
        )
        filters.append(f"pad={options.width}:{options.height}:(ow-iw)/2:(oh-ih)/2")
    else:
        filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
    cmd += [
        "-vf",
        ",".join(filters),
        "-map",
        "0:v:0",
        "-map",
        "0:a",
        "-map_metadata",
        "0",
    ]
    codecs = [
        s["codec_name"] for s in info["raw"]["streams"] if s["codec_type"] == "audio"
    ]
    for i, codec in enumerate(codecs):
        cmd += [
            f"-c:a:{i}",
            "copy" if codec in ("aac", "mp3", "ac3", "eac3", "alac") else "aac",
        ]
    cmd += [
        "-c:v",
        "libx264",
        "-crf",
        str(options.crf),
        "-preset",
        options.preset,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        target,
    ]
    run(cmd, log=folder / "render.log", cwd=folder, timeout=timeout_s)

    # A complex 720p music video can become >100 MB after subtitle burn.
    # Only if necessary, rerender from the ORIGINAL video + ASS with a
    # bitrate budget. Never recompress the first lossy render or its audio.
    # Keep H.264 for broad TV compatibility; avoid HEVC-only downloads.
    if target.stat().st_size > MAX_KARAOKE_BYTES and options.crf == 23:
        duration = float(info["duration"])
        audio_bps = sum(
            int(stream["bit_rate"])
            if str(stream.get("bit_rate") or "").isdecimal()
            else 192000
            for stream in info["raw"]["streams"]
            if stream["codec_type"] == "audio"
        )
        video_bps = max(
            250000, int(COMPACT_TARGET_BYTES * 8 / duration - audio_bps - 20000)
        )
        compact = target.with_name(target.stem + ".compact.mp4")
        compact_cmd = [
            *cmd[: cmd.index("-c:v")],
            "-c:v",
            "libx264",
            "-b:v",
            str(video_bps),
            "-maxrate",
            str(int(video_bps * 1.2)),
            "-bufsize",
            str(video_bps * 2),
            "-preset",
            options.preset,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            compact,
        ]
        try:
            run(
                compact_cmd,
                log=folder / "compact.log",
                cwd=folder,
                timeout=timeout_s,
            )
            if 1024 < compact.stat().st_size < target.stat().st_size:
                compact.replace(target)
        except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
            log.warning(
                "Video compaction failed; keeping playable original render",
                exc_info=True,
            )
        finally:
            compact.unlink(missing_ok=True)
    return target
