"""Independent, bounded sources of user-selected lyric candidates.

No song/artist matching is performed: the guest chooses the correct version.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ktv_app.lyric_names import title_variants
from ktv_app.text_normalization import clean_lyric_text

from .lyric_sources import vcpedia_lyric_candidates

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "VC-KTV/0.5 (guest lyrics search)"}
TIMEOUT = httpx.Timeout(3.5, connect=2)
DEADLINE = 4.0


def _candidate(
    title: str, source: str, raw: str, url: str, artist: str = ""
) -> dict | None:
    """Only validate that a source supplied usable lyric lines."""
    if not title.strip() or not isinstance(raw, str):
        return None
    text = clean_lyric_text(raw).strip()[:12_000]
    if len(text.splitlines()) < 2:
        return None
    result = {"title": title, "source": source, "text": text, "url": url}
    unfiltered = clean_lyric_text(raw, keep_leading_credits=True).strip()[:12_000]
    if unfiltered != text and len(unfiltered.splitlines()) >= 2:
        # Users can inspect and restore questionable leading credits.
        result["unfiltered_text"] = unfiltered
    if artist:
        result["artist"] = artist
    return result


async def _wiki_titles(query: str) -> list[str]:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
        response = await client.get(
            "https://vcpedia.cn/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": 4,
                "format": "json",
            },
        )
        response.raise_for_status()
    return [
        item["title"]
        for item in response.json().get("query", {}).get("search", [])
        if isinstance(item.get("title"), str)
    ]


async def _lrclib(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
        response = await client.get(
            "https://lrclib.net/api/search", params={"track_name": query}
        )
        response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        return []
    output = []
    for row in rows[:8]:
        if not isinstance(row, dict) or row.get("instrumental"):
            continue
        title, lyrics = (
            row.get("trackName"),
            row.get("plainLyrics") or row.get("syncedLyrics"),
        )
        if not isinstance(title, str):
            continue
        url = (
            f"https://lrclib.net/api/get/{row['id']}"
            if isinstance(row.get("id"), int)
            else "https://lrclib.net/"
        )
        item = _candidate(title, "LRCLIB", lyrics, url, row.get("artistName") or "")
        if item:
            output.append(item)
    return output[:6]


async def _netease(query: str) -> list[dict]:
    """Public catalogue order, no title-similarity or artist-based rejection."""
    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers=HEADERS, follow_redirects=False
    ) as client:
        response = await client.get(
            "https://music.163.com/api/search/get",
            params={"s": query, "type": 1, "limit": 6},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 200:
            return []
        songs = (data.get("result") or {}).get("songs") or []
        if not isinstance(songs, list):
            return []
        tracks = [
            s for s in songs[:6] if isinstance(s, dict) and isinstance(s.get("id"), int)
        ]

        async def lyric(track):
            try:
                response = await client.get(
                    "https://music.163.com/api/song/lyric",
                    params={"id": track["id"], "lv": -1, "kv": -1, "tv": -1},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 200 or payload.get("nolyric"):
                    return None
                raw = (payload.get("lrc") or {}).get("lyric")
                name = track.get("name")
                if not isinstance(name, str):
                    return None
                artists = track.get("artists") or []
                artist = " / ".join(
                    a["name"]
                    for a in artists
                    if isinstance(a, dict) and isinstance(a.get("name"), str)
                )
                return _candidate(
                    name,
                    "网易云音乐",
                    raw,
                    f"https://music.163.com/song?id={track['id']}",
                    artist,
                )
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                return None

        return [
            c for c in await asyncio.gather(*(lyric(track) for track in tracks)) if c
        ]


async def _collect_ready(coroutines) -> list[dict]:
    """Keep finished candidate pages when one sibling page stalls."""
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=3.0)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    result = []
    for task in tasks:
        if task in done and not task.cancelled() and task.exception() is None:
            result.extend(task.result())
    return result[:6]


async def _wiki_search(query: str) -> list[dict]:
    titles = list(dict.fromkeys(await _wiki_titles(query)))[:4]
    # The actual MediaWiki page may use alternate glyphs.
    return await _collect_ready(vcpedia_lyric_candidates(title) for title in titles)


async def _more_candidates(query: str) -> list[dict]:
    """Extra LRCLIB name variants, not slow general web scraping."""
    return await _collect_ready(_lrclib(title) for title in title_variants(query)[:2])


async def search_lyric_candidates(query: str, phase: str = "all") -> list[dict]:
    query = query.strip()
    if not 1 <= len(query) <= 60:
        return []
    sources = {
        "vcpedia": lambda: vcpedia_lyric_candidates(query),
        "wiki": lambda: _wiki_search(query),
        "lrclib": lambda: _lrclib(query),
        "netease": lambda: _netease(query),
        "more": lambda: _more_candidates(query),
    }
    if phase == "all":
        batches = await asyncio.gather(
            *(
                search_lyric_candidates(query, name)
                for name in ("vcpedia", "wiki", "lrclib", "netease")
            )
        )
        seen, results = set(), []
        for batch in batches:
            for item in batch:
                key = (item["title"], item["text"])
                if key not in seen:
                    seen.add(key)
                    results.append(item)
        return results[:12]
    if phase not in sources:
        raise ValueError("unsupported lyric search phase")
    try:
        return (await asyncio.wait_for(sources[phase](), timeout=DEADLINE))[:6]
    except Exception as exc:  # noqa: BLE001 - one failed source must not block others
        log.info("Lyric source %s unavailable (%s)", phase, type(exc).__name__)
        return []
