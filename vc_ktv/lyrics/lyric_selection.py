"""Independent, bounded sources of user-selected lyric candidates.

No song/artist matching is performed: the guest chooses the correct version.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from vc_ktv.lyrics.lyric_sources import vcpedia_lyric_candidates
from vc_ktv.lyrics.timed_lyrics import provider_timeline
from vc_ktv.pipeline import clean_lyric_text, title_variants

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "VC-KTV/0.5 (guest lyrics search)"}
TIMEOUT = httpx.Timeout(15, connect=5)
DEADLINE = 16.0


def _candidate(
    title: str, source: str, raw: str, url: str, artist: str = ""
) -> dict | None:
    """Only validate that a source supplied usable lyric lines."""
    if not title.strip() or not isinstance(raw, str):
        return None
    text = clean_lyric_text(raw).strip()
    if len(text.splitlines()) < 2:
        return None
    result = {"title": title, "source": source, "text": text, "url": url}
    if timed := provider_timeline(raw, text):
        result["timed_lrc"] = timed
        result["source_lrc_raw"] = raw
    unfiltered = clean_lyric_text(raw, keep_leading_credits=True).strip()
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
                "srlimit": 30,
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
    for row in rows:
        if not isinstance(row, dict) or row.get("instrumental"):
            continue
        title, lyrics = (
            row.get("trackName"),
            row.get("syncedLyrics") or row.get("plainLyrics"),
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
    return output


async def netease_song_lyrics(
    song_id: int, *, title: str = "", transport: httpx.AsyncBaseTransport | None = None
) -> list[dict]:
    """Prefer lyrics from the exact selected NetEase song ID, not a title match.

    No account Cookie or audio URL is involved. Keep the other source choices
    available to the user if this particular version has no usable lyrics.
    """
    if (
        not isinstance(song_id, int)
        or isinstance(song_id, bool)
        or not 0 < song_id < 2**63
    ):
        return []
    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers=HEADERS, follow_redirects=False, transport=transport
    ) as client:
        resp = await client.get(
            "https://music.163.com/api/song/lyric",
            params={"id": song_id, "lv": -1, "kv": -1, "tv": -1},
        )
        resp.raise_for_status()
        payload = resp.json()
    if not isinstance(payload, dict) or payload.get("code") != 200:
        return []
    raw = (payload.get("lrc") or {}).get("lyric")
    if not isinstance(raw, str):
        return []
    return (
        [candidate]
        if (
            candidate := _candidate(
                title or "当前网易云单曲",
                "网易云音乐",
                raw,
                f"https://music.163.com/song?id={song_id}",
            )
        )
        else []
    )


async def _netease(query: str) -> list[dict]:
    """Public catalogue order, no title-similarity or artist-based rejection."""
    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers=HEADERS, follow_redirects=False
    ) as client:
        response = await client.get(
            "https://music.163.com/api/search/get",
            params={"s": query, "type": 1, "limit": 15},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 200:
            return []
        songs = (data.get("result") or {}).get("songs") or []
        if not isinstance(songs, list):
            return []
        tracks = [
            s for s in songs if isinstance(s, dict) and isinstance(s.get("id"), int)
        ]

        async def lyric(track):
            try:
                response = await client.get(
                    "https://music.163.com/api/song/lyric",
                    params={"id": track["id"], "lv": -1, "kv": -1, "tv": -1},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 200:
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
    # Return completed articles before the outer source deadline expires.
    done, pending = await asyncio.wait(tasks, timeout=DEADLINE * 0.75)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    result = []
    for task in tasks:
        if task in done and not task.cancelled() and task.exception() is None:
            result.extend(task.result())
    return result


async def _wiki_search(query: str) -> list[dict]:
    titles = list(dict.fromkeys(await _wiki_titles(query)))
    # The actual MediaWiki page may use alternate glyphs.
    return await _collect_ready(vcpedia_lyric_candidates(title) for title in titles)


async def _more_candidates(query: str) -> list[dict]:
    """Extra LRCLIB name variants, not slow general web scraping."""
    return await _collect_ready(_lrclib(title) for title in title_variants(query))


async def search_lyric_candidates(query: str, phase: str = "all") -> list[dict]:
    query = query.strip()
    if not query:
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
                # Identically worded lyrics can belong to different recordings,
                # with different (or missing) timing. Keep those choices distinct.
                key = (item["source"], item.get("url"), item["text"])
                if key not in seen:
                    seen.add(key)
                    results.append(item)
        return results
    if phase not in sources:
        raise ValueError("unsupported lyric search phase")
    try:
        return await asyncio.wait_for(sources[phase](), timeout=DEADLINE)
    except Exception as exc:  # noqa: BLE001 - one failed source must not block others
        log.info("Lyric source %s unavailable (%s)", phase, type(exc).__name__)
        return []
