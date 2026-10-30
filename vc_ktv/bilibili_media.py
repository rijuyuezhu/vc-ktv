"""Fallback for public Bilibili videos when yt-dlp's webpage extractor breaks.

Only documented public, unauthenticated metadata/playurl responses are used.
No cookies, paid streams, encryption circumvention, or third-party mirrors.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)
BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
LIMIT = 512 * 1024 * 1024
TRUSTED_CDN = ("bilivideo.com", "bilivideo.cn", "hdslb.com")
# Public video quality ceilings exposed to admins: 720p (default), 480p.
QUALITY_LEVELS = ((64, 720), (32, 480))
QUALITY_HEIGHTS = tuple(height for _, height in QUALITY_LEVELS)


class PublicMediaUnavailable(RuntimeError):
    pass


def _allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and parsed.port in (None, 443)
        and any(host == suffix or host.endswith("." + suffix) for suffix in TRUSTED_CDN)
    )


def _json(client: httpx.Client, path: str, params: dict) -> dict:
    response = client.get("https://api.bilibili.com/" + path, params=params)
    response.raise_for_status()
    result = response.json()
    if result.get("code") != 0 or not isinstance(result.get("data"), dict):
        raise PublicMediaUnavailable(
            "B站公开播放接口未授权或不可用"
            + (f"（code {result.get('code')}）" if "code" in result else "")
        )
    return result["data"]


def _download(client: httpx.Client, urls: list[str], dest: Path, max_size: int) -> int:
    errors = []
    for url in urls:
        if not _allowed(url):
            errors.append("未经信任的媒体来源")
            continue
        try:
            with client.stream("GET", url, follow_redirects=False) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise PublicMediaUnavailable(
                        f"媒体服务器返回 HTTP {response.status_code}"
                    )
                declared = int(response.headers.get("content-length") or 0)
                if declared > max_size:
                    raise PublicMediaUnavailable("媒体体积超过限制")
                total = 0
                with dest.open("wb") as out:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > max_size:
                            raise PublicMediaUnavailable("媒体体积超过限制")
                        out.write(chunk)
                if total < 1024:
                    raise PublicMediaUnavailable("媒体服务器返回的内容太短")
                return total
        except (httpx.HTTPError, ValueError, OSError, PublicMediaUnavailable) as e:
            dest.unlink(missing_ok=True)
            errors.append(type(e).__name__)
    raise PublicMediaUnavailable("所有公开媒体 CDN 均失败：" + ",".join(errors[:4]))


def public_media_download(
    bvid: str, page: int, destdir: Path, *, max_height: int = 720
) -> Path:
    """Prefer public DASH AVC/AAC at the selected 720p or 480p ceiling."""
    if not BVID.fullmatch(bvid) or page < 1:
        raise ValueError("无效 BV 号或分 P")
    if max_height not in QUALITY_HEIGHTS:
        raise ValueError("不支持的清晰度")
    levels = [(qn, height) for qn, height in QUALITY_LEVELS if height <= max_height]
    failures = []
    with httpx.Client(headers=HEADERS, timeout=httpx.Timeout(35, connect=10)) as client:
        view = _json(client, "x/web-interface/view", {"bvid": bvid})
        pages = view.get("pages") or []
        if page > len(pages) or not isinstance(pages[page - 1].get("cid"), int):
            raise PublicMediaUnavailable("B站视频分 P 不存在")
        cid = pages[page - 1]["cid"]

        # DASH often lists several lower qualities in one response. If the
        # requested stream fails or is restricted, try the next available AVC
        # representation before asking for a lower qn response.
        attempted = set()
        for qn, height in levels:
            try:
                data = _json(
                    client,
                    "x/player/playurl",
                    {"bvid": bvid, "cid": cid, "qn": qn, "fnval": 16, "fourk": 0},
                )
            except (httpx.HTTPError, ValueError, PublicMediaUnavailable) as error:
                failures.append(f"DASH {height}p: {type(error).__name__}")
                continue
            dash = data.get("dash") or {}
            videos = sorted(
                (
                    item
                    for item in dash.get("video", [])
                    if str(item.get("codecs", "")).startswith("avc1")
                    and isinstance(item.get("id"), int)
                    and item["id"] <= qn
                ),
                key=lambda item: (item["id"], item.get("bandwidth", 0)),
                reverse=True,
            )
            audios = sorted(
                (
                    item
                    for item in dash.get("audio", [])
                    if str(item.get("codecs", "")).startswith("mp4a")
                ),
                key=lambda item: item.get("bandwidth", 0),
                reverse=True,
            )
            if not audios:
                continue
            audio = audios[0]
            for video in videos:
                # qn requests may return identical lower-quality streams.
                stream = video.get("baseUrl")
                if not stream or stream in attempted:
                    continue
                attempted.add(stream)
                try:
                    vsize = _download(
                        client,
                        [stream, *(video.get("backupUrl") or [])],
                        destdir / "video.m4s",
                        LIMIT,
                    )
                    _download(
                        client,
                        [audio.get("baseUrl", ""), *(audio.get("backupUrl") or [])],
                        destdir / "audio.m4s",
                        LIMIT - vsize,
                    )
                    output = destdir / "source.mp4"
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-nostdin",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-i",
                            str(destdir / "video.m4s"),
                            "-i",
                            str(destdir / "audio.m4s"),
                            "-map",
                            "0:v:0",
                            "-map",
                            "1:a:0",
                            "-c",
                            "copy",
                            "-movflags",
                            "+faststart",
                            str(output),
                        ],
                        timeout=75,
                        check=True,
                        capture_output=True,
                    )
                    if output.stat().st_size > LIMIT:
                        raise PublicMediaUnavailable("封装后体积超过限制")
                    log.info(
                        "Bilibili public DASH video qn=%s (requested <=%sp) succeeded for %s p%s",
                        video["id"],
                        height,
                        bvid,
                        page,
                    )
                    return output
                except (
                    httpx.HTTPError,
                    OSError,
                    subprocess.SubprocessError,
                    PublicMediaUnavailable,
                ) as error:
                    failures.append(f"DASH qn={video['id']}: {type(error).__name__}")
                    for name in ("video.m4s", "audio.m4s", "source.mp4"):
                        (destdir / name).unlink(missing_ok=True)

        # Some older uploads only have one progressive MP4 representation.
        for qn, height in levels:
            try:
                data = _json(
                    client,
                    "x/player/playurl",
                    {"bvid": bvid, "cid": cid, "qn": qn, "fnval": 0},
                )
                parts = data.get("durl") or []
                if len(parts) != 1:
                    continue
                target = destdir / "source.mp4"
                _download(
                    client,
                    [parts[0].get("url", ""), *(parts[0].get("backup_url") or [])],
                    target,
                    LIMIT,
                )
                log.info(
                    "Bilibili public progressive <=%sp succeeded for %s p%s",
                    height,
                    bvid,
                    page,
                )
                return target
            except (httpx.HTTPError, ValueError, PublicMediaUnavailable) as error:
                failures.append(f"MP4 {height}p: {type(error).__name__}")
    raise PublicMediaUnavailable(
        "该视频没有公开可用的 H.264/AAC 或 MP4 媒体"
        + (f"；{', '.join(failures[:4])}" if failures else "")
    )
