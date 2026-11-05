"""Fetch an available NetEase track and build a standard KTV-compatible MP4.

Only the fixed official catalogue endpoint is queried; the media/CDN and cover
hosts are validated. No DRM bypass or alternate-source substitution is used.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .netease_catalog import HEADERS, _cover_url

MAX_AUDIO_BYTES = 80 * 1024 * 1024
MAX_COVER_BYTES = 4 * 1024 * 1024


class MusicUnavailable(ValueError):
    """The requested track has no full playable stream via this account."""


def validate_media_url(value: str) -> str:
    """Accept only HTTPS media URLs from known NetEase media CDN hosts."""
    try:
        p = urlsplit(value)
        if (
            p.scheme != "https"
            or p.username is not None
            or p.password is not None
            or p.port is not None
            or not re.fullmatch(r"(?:m\d+|m)\.music\.126\.net", p.hostname or "")
        ):
            raise ValueError("网易云返回了不受信任的媒体地址")
    except (TypeError, ValueError) as error:
        raise ValueError("网易云返回了不受信任的媒体地址") from error
    return value


def parse_audio_response(song_id: int, payload: dict) -> str:
    """Distinguish full media from unavailable or trial-only audio."""
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise MusicUnavailable("网易云音源暂不可用")
    track = next(
        (v for v in items if isinstance(v, dict) and v.get("id") == song_id), None
    )
    if not track or track.get("code") != 200 or track.get("freeTrialInfo") is not None:
        raise MusicUnavailable("此歌曲暂时无法获取完整音频")
    url = track.get("url")
    if not url:
        raise MusicUnavailable("此歌曲没有可用的完整音频")
    return validate_media_url(url)


def fetch_audio_url(
    song_id: int,
    *,
    cookies: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Request the normal NetEase audio endpoint; do not log the signed URL."""
    with httpx.Client(
        timeout=12,
        headers=HEADERS,
        follow_redirects=False,
        cookies=cookies or {},
        transport=transport,
    ) as client:
        response = client.get(
            "https://music.163.com/api/song/enhance/player/url",
            params={"ids": json.dumps([song_id]), "br": 320000},
        )
        response.raise_for_status()
        return parse_audio_response(song_id, response.json())


def download_limited(client: httpx.Client, url: str, path: Path, limit: int) -> None:
    """Stream with a strict size bound, no redirects, and no persisted URL."""
    with client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise MusicUnavailable("网易云音频地址已失效")
        size = 0
        with path.open("wb") as out:
            for chunk in response.iter_bytes(256 * 1024):
                size += len(chunk)
                if size > limit:
                    raise MusicUnavailable("媒体大小超过限制")
                out.write(chunk)
        if size < 1024:
            raise MusicUnavailable("音频或封面内容为空")


def _audio_duration(audio: Path) -> float:
    result = subprocess.run(
        [
            os.environ.get("KTV_FFPROBE", "ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return float(result.stdout.strip())


def assemble_album_video(
    audio: Path, cover: Path | None, dest: Path, duration: float
) -> Path:
    """Minimal 720p MP4 source that the existing subtitle and TV pipeline accepts."""
    ffmpeg = os.environ.get("KTV_FFMPEG", "ffmpeg")
    args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if cover:
        args += ["-loop", "1", "-framerate", "24", "-i", str(cover)]
        # Static cover in a subtle full-bleed frame. Video animation is not a
        # prerequisite for this first working end-to-end path.
        vf = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x11192d,format=yuv420p"
    else:
        args += ["-f", "lavfi", "-i", "color=c=0x11192d:s=1280x720:r=24"]
        vf = "format=yuv420p"
    args += [
        "-i",
        str(audio),
        "-t",
        str(duration),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "27",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "24",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    subprocess.run(args, capture_output=True, check=True, timeout=900)
    if not dest.is_file() or dest.stat().st_size < 1024:
        raise MusicUnavailable("无法生成网易云视频")
    return dest


def download_netease_video(
    song_id: int,
    cover_url: str,
    target_dir: Path,
    *,
    duration: float,
    cookies: dict[str, str] | None = None,
) -> Path:
    """Fetch a normal full stream, validate duration, encode a playable MP4."""
    target_dir.mkdir(parents=True, exist_ok=True)
    url = fetch_audio_url(song_id, cookies=cookies)
    audio = target_dir / "netease-audio.bin"
    cover = target_dir / "netease-cover.jpg"
    with httpx.Client(timeout=httpx.Timeout(45, connect=8), headers=HEADERS) as client:
        download_limited(client, url, audio, MAX_AUDIO_BYTES)
        checked_cover = _cover_url(cover_url)
        if checked_cover:
            try:
                download_limited(client, checked_cover, cover, MAX_COVER_BYTES)
            except (httpx.HTTPError, MusicUnavailable, OSError):
                cover.unlink(missing_ok=True)
    actual = _audio_duration(audio)
    if not 0 < actual <= 3600 or actual < duration * 0.8:
        raise MusicUnavailable("网易云返回的是试听片段或不完整音频")
    return assemble_album_video(
        audio, cover if cover.exists() else None, target_dir / "source.mp4", actual
    )
