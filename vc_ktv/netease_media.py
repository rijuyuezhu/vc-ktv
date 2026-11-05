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

from .disk_budget import check_current_budget
from .netease_catalog import HEADERS, _cover_url
from .netease_weapi import encode_weapi_form


class MusicUnavailable(ValueError):
    """The requested track has no full playable stream via this account."""


def validate_media_url(value: str) -> str:
    """Accept NetEase-signed HTTP/HTTPS audio URLs only on its fixed CDN host pattern.

    NetEase currently returns HTTP URLs for legitimate tracks. Never follow
    redirects or fetch arbitrary hosts supplied by a user.
    """
    try:
        p = urlsplit(value)
        if (
            p.scheme not in {"http", "https"}
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
    if isinstance(items, dict):
        items = [items]  # Official download/v1 returns one object, player/v1 a list.
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
    level: str = "exhigh",
) -> str:
    """Resolve a full track via NetEase's regular authenticated web player API."""
    if level not in {"exhigh", "higher", "standard"}:
        raise ValueError("不支持的网易云音质等级")
    with httpx.Client(
        timeout=12,
        headers=HEADERS,
        follow_redirects=False,
        # NetEase signs CDN links according to the web client's cookie context.
        # go-musicfox includes these cookies even when MUSIC_U is already valid.
        cookies={"os": "pc", "__remember_me": "true", **(cookies or {})},
        transport=transport,
    ) as client:
        response = client.post(
            "https://music.163.com/weapi/song/enhance/player/url/v1",
            data=encode_weapi_form(
                {"ids": json.dumps([song_id]), "level": level, "encodeType": "flac"}
            ),
            headers={"Origin": "https://music.163.com"},
        )
        response.raise_for_status()
        return parse_audio_response(song_id, response.json())


def fetch_download_url(
    song_id: int,
    *,
    cookies: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Resolve the official download endpoint when player CDN links are rejected."""
    with httpx.Client(
        timeout=12,
        headers=HEADERS,
        follow_redirects=False,
        # NetEase signs CDN links according to the web client's cookie context.
        # go-musicfox includes these cookies even when MUSIC_U is already valid.
        cookies={"os": "pc", "__remember_me": "true", **(cookies or {})},
        transport=transport,
    ) as client:
        response = client.post(
            "https://music.163.com/weapi/song/enhance/download/url/v1",
            data=encode_weapi_form({"id": song_id, "level": "standard"}),
            headers={"Origin": "https://music.163.com"},
        )
        response.raise_for_status()
        return parse_audio_response(song_id, response.json())


def download_limited(
    client: httpx.Client, url: str, path: Path, limit: int | None
) -> None:
    """Stream media with an optional size bound, no redirects or persisted URL."""
    # NetEase CDN regularly serves byte-range downloads but can reject a
    # plain GET. Ask for the complete file starting at byte zero.
    with client.stream(
        "GET", url, headers={"Range": "bytes=0-"}, follow_redirects=False
    ) as response:
        if response.status_code not in (200, 206):
            raise MusicUnavailable(f"网易云媒体下载失败（HTTP {response.status_code}）")
        if "text/html" in response.headers.get("content-type", "").lower():
            raise MusicUnavailable("网易云媒体地址返回了网页，无法下载音频")
        expected = None
        if response.status_code == 206:
            match = re.fullmatch(
                r"bytes 0-(\d+)/(\d+)", response.headers.get("content-range", "")
            )
            if not match or int(match[1]) + 1 != int(match[2]):
                raise MusicUnavailable("网易云只返回了部分媒体内容")
            expected = int(match[2])
            if limit is not None and expected > limit:
                raise MusicUnavailable("媒体大小超过限制")
        size = 0
        temporary = path.with_name(path.name + ".part")
        try:
            with temporary.open("wb") as out:
                next_check = 16 * 1024 * 1024
                for chunk in response.iter_bytes(256 * 1024):
                    size += len(chunk)
                    if limit is not None and size > limit:
                        raise MusicUnavailable("媒体大小超过限制")
                    out.write(chunk)
                    if size >= next_check:
                        check_current_budget()
                        next_check = size + 16 * 1024 * 1024
                check_current_budget()
            if size < 1024 or (expected is not None and size != expected):
                raise MusicUnavailable("音频或封面内容不完整")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


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
    subprocess.run(args, capture_output=True, check=True)
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
    audio = target_dir / "netease-audio.bin"
    cover = target_dir / "netease-cover.jpg"
    with httpx.Client(timeout=httpx.Timeout(45, connect=8), headers=HEADERS) as client:
        # An individual CDN edge can reject a signed URL even when the track
        # is available. Resolve a fresh URL at lower qualities before failing.
        last_error: Exception | None = None
        for level in ("exhigh", "higher", "standard"):
            try:
                url = fetch_audio_url(song_id, cookies=cookies, level=level)
                download_limited(client, url, audio, None)
                break
            except (MusicUnavailable, httpx.HTTPError) as error:
                # A lower offered quality may have a different signed CDN URL.
                # Do not retry arbitrary hosts, bypass restrictions or accept trials.
                last_error = error
        else:
            # The official download API can issue a different valid CDN URL
            # for the same authorized track. Keep it as the final bounded fallback.
            try:
                url = fetch_download_url(song_id, cookies=cookies)
                download_limited(client, url, audio, None)
            except (MusicUnavailable, httpx.HTTPError) as error:
                if isinstance(error, MusicUnavailable):
                    raise
                if isinstance(last_error, MusicUnavailable):
                    raise last_error from error
                raise MusicUnavailable("网易云媒体服务暂时不可用") from error
        checked_cover = _cover_url(cover_url)
        if checked_cover:
            try:
                download_limited(client, checked_cover, cover, None)
            except (httpx.HTTPError, MusicUnavailable, OSError):
                cover.unlink(missing_ok=True)
    actual = _audio_duration(audio)
    if actual <= 0 or actual < duration * 0.8:
        raise MusicUnavailable("网易云返回的是试听片段或不完整音频")
    return assemble_album_video(
        audio, cover if cover.exists() else None, target_dir / "source.mp4", actual
    )
