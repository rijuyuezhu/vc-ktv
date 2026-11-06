"""Resolve official song shares and search public NetEase metadata.

No account cookies, stream URLs, user-supplied request hosts, or playlist IDs.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

from .netease_catalog import HEADERS, normalize_song_metadata
from .netease_source import parse_netease_song_id

SHARE_HOST = "163cn.tv"
SHARE_PATH = re.compile(r"/[A-Za-z0-9_-]{4,40}\Z")
SHARE_URL = re.compile(r"https?://[^\s<>\"'“”‘’（）()「」]+", re.I)
MAX_SHARE_TEXT = 2048
SEARCH_PAGE_SIZE = 30


def extract_song_reference(value: str) -> str:
    """Extract one URL from a pasted NetEase share sentence, or a bare ID."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_SHARE_TEXT:
        raise ValueError("请输入网易云单曲 ID 或分享链接")
    raw = value.strip()
    if raw.isascii() and raw.isdecimal():
        return raw
    links = [m.group(0).rstrip(".,，。!！;；、") for m in SHARE_URL.finditer(raw)]
    if len(links) != 1:
        raise ValueError("请粘贴包含一个单曲链接的分享内容")
    return links[0]


def _short_share_url(value: str) -> str | None:
    try:
        url = urlsplit(value)
        if url.hostname != SHARE_HOST:
            return None
        if (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.port is not None
            or url.query
            or url.fragment
            or not SHARE_PATH.fullmatch(url.path)
        ):
            raise ValueError("网易云分享短链格式不正确")
        # Only allow a well-formed path to the fixed, trusted short-link host.
        return f"https://{SHARE_HOST}{url.path}"
    except ValueError as error:
        raise ValueError("网易云分享短链格式不正确") from error


async def resolve_song_reference(
    value: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    """Resolve just the official 163cn.tv share-link redirect, never arbitrary URLs."""
    ref = extract_song_reference(value)
    short_url = _short_share_url(ref)
    if short_url is None:
        return parse_netease_song_id(ref)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(8, connect=3),
        follow_redirects=False,
        transport=transport,
        headers={"User-Agent": HEADERS["User-Agent"]},
    ) as client:
        response = await client.get(short_url)
        if response.status_code not in (301, 302, 303, 307, 308):
            raise ValueError("网易云分享链接暂时无法解析")
        target = response.headers.get("location", "")
        # The song URL must be one of the already validated official hosts.
        return parse_netease_song_id(target)


async def search_netease_songs(
    query: str, *, page: int = 0, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict]:
    """Return bounded public single-song results, no playable/audio URLs."""
    term = query.strip()
    if not term or page < 0:
        raise ValueError("请输入歌名或歌手")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(8, connect=3),
        headers=HEADERS,
        follow_redirects=False,
        transport=transport,
    ) as client:
        response = await client.get(
            "https://music.163.com/api/search/get",
            params={
                "s": term,
                "type": "1",
                "limit": str(SEARCH_PAGE_SIZE),
                "offset": str(page * SEARCH_PAGE_SIZE),
            },
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise ValueError("网易云搜索响应异常")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("网易云搜索响应异常")
    if result.get("songCount") == 0 and not result.get("songs"):
        return []
    if not isinstance(result.get("songs"), list):
        raise ValueError("网易云搜索响应异常")
    songs = []
    for track in result["songs"]:
        if not isinstance(track, dict) or not isinstance(track.get("id"), int):
            continue
        try:
            songs.append(
                normalize_song_metadata(track["id"], {"code": 200, "songs": [track]})
            )
        except (ValueError, LookupError):
            continue
    return songs
