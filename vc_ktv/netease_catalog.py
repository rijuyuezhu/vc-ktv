"""Read-only public song metadata. No login, stream URL, or music downloads."""

import math
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

CATALOG_URL = "https://music.163.com/api/song/detail/"
HEADERS = {
    "User-Agent": "VC-KTV/0.5 (music metadata preview)",
    "Referer": "https://music.163.com/",
}


def _cover_url(url: object) -> str:
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or not re.fullmatch(r"p\d+\.music\.126\.net", parsed.hostname or "")
        ):
            return ""
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return ""


def normalize_song_metadata(song_id: int, payload: dict) -> dict:
    """Bound and validate catalogue data before returning it to the UI."""
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise ValueError("网易云歌曲信息响应异常")
    songs = payload.get("songs")
    if not isinstance(songs, list):
        raise ValueError("网易云歌曲信息响应异常")
    track = next(
        (s for s in songs if isinstance(s, dict) and s.get("id") == song_id), None
    )
    if track is None:
        raise LookupError("网易云未找到该单曲")
    title = track.get("name")
    duration_ms = track.get("duration", track.get("dt"))
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or duration_ms <= 0
    ):
        raise ValueError("网易云歌曲信息不完整")
    artists = track.get("artists", track.get("ar")) or []
    names = (
        [
            a["name"].strip()
            for a in artists
            if isinstance(a, dict)
            and isinstance(a.get("name"), str)
            and a["name"].strip()
        ]
        if isinstance(artists, list)
        else []
    )
    album = track.get("album", track.get("al")) or {}
    cover = _cover_url(album.get("picUrl")) if isinstance(album, dict) else ""
    return {
        "source": "netease",
        "source_id": str(song_id),
        "title": title.strip()[:200],
        "uploader": (" / ".join(names) or "未知歌手")[:200],
        "cover": cover,
        "duration": math.ceil(duration_ms / 1000),
        "official_url": f"https://music.163.com/#/song?id={song_id}",
    }


async def lookup_netease_song(
    song_id: int, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict:
    """Request only the fixed catalogue endpoint with a validated numeric ID."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(8, connect=3),
        headers=HEADERS,
        follow_redirects=False,
        transport=transport,
    ) as client:
        response = await client.get(
            CATALOG_URL, params={"id": str(song_id), "ids": f"[{song_id}]"}
        )
        response.raise_for_status()
        return normalize_song_metadata(song_id, response.json())
