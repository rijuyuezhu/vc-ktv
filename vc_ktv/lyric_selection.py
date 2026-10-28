"""Bounded lyric candidates for the submitter's selection."""

from __future__ import annotations

import asyncio
import re

import httpx

from ktv_app.lyric_names import similarity, title_variants
from ktv_app.lyric_netease import lookup_netease
from ktv_app.lyric_sources import lookup_sites
from ktv_app.text_normalization import clean_lyric_text
from ktv_app.web_search import search_web

from .lyric_sources import vcpedia_lyric_candidates

HEADERS = {"User-Agent": "VC-KTV/0.5 (guest lyrics search)"}
TIMEOUT = httpx.Timeout(10, connect=5)


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
    result = []
    for row in rows[:8]:
        if not isinstance(row, dict) or row.get("instrumental"):
            continue
        text = row.get("plainLyrics") or row.get("syncedLyrics") or ""
        title = row.get("trackName")
        if not isinstance(text, str) or not isinstance(title, str):
            continue
        text = re.sub(r"^\[\d+:\d+(?:\.\d+)?\]", "", text, flags=re.M).strip()
        if len(text.splitlines()) >= 2 and 2 <= len(title) <= 90:
            result.append(
                {
                    "title": title,
                    "source": "LRCLIB",
                    "text": text[:12_000],
                    "url": f"https://lrclib.net/api/get/{row['id']}"
                    if isinstance(row.get("id"), int)
                    else "https://lrclib.net/",
                }
            )
    return result


def _external_candidates(records: list[dict], query: str) -> list[dict]:
    """Adapt upstream source records to the existing guest-choice contract."""
    result = []
    for item in records:
        title, artist = item.get("trackName"), item.get("artistName")
        source = item.get("provider")
        url = item.get("source")
        raw = item.get("plainLyrics") or item.get("syncedLyrics")
        if (
            not isinstance(title, str)
            or not isinstance(artist, str)
            or not isinstance(source, str)
            or not isinstance(url, str)
            or not isinstance(raw, str)
            or similarity(query, title) < 0.5
        ):
            continue
        text = clean_lyric_text(raw).strip()[:12_000]
        if len(text.splitlines()) < 2:
            continue
        result.append(
            {
                "title": title,
                "artist": artist,
                "source": source,
                "url": url,
                "text": text,
            }
        )
    return result


async def search_lyric_candidates(query: str) -> list[dict]:
    """Retrieve diverse, source-labeled candidates; never select for a guest."""
    query = query.strip()
    if not 2 <= len(query) <= 60:
        return []
    sources = await asyncio.gather(
        _wiki_titles(query), _lrclib(query), return_exceptions=True
    )
    titles = [query]
    if isinstance(sources[0], list):
        titles.extend(sources[0])
    titles = list(dict.fromkeys(titles))[:5]
    wiki = await asyncio.gather(
        *(vcpedia_lyric_candidates(title) for title in titles),
        return_exceptions=True,
    )
    groups: dict[str, list[dict]] = {
        "VCPedia": [
            candidate
            for batch in wiki
            if isinstance(batch, list)
            for candidate in batch
        ],
        "LRCLIB": sources[1] if isinstance(sources[1], list) else [],
    }
    seen = set()

    def valid(candidate):
        if not isinstance(candidate, dict):
            return None
        title, raw = candidate.get("title"), candidate.get("text")
        if not isinstance(title, str) or not isinstance(raw, str):
            return None
        text = raw.strip()[:12_000]
        if len(text.splitlines()) < 2:
            return None
        return {**candidate, "text": text}

    unique_primary = {
        (c["title"], c["text"])
        for group in groups.values()
        for candidate in group
        if (c := valid(candidate))
    }
    primary_sources = sum(
        any(valid(candidate) for candidate in group) for group in groups.values()
    )
    # A single provider can return dozens of similar variants.
    # Try other hosts even when one provider fills all six UI slots.
    if len(unique_primary) < 6 or primary_sources < 2:
        variants = title_variants(query)[:2]
        tasks = [*(_lrclib(v) for v in variants)]
        # All external sync retrievals stay off the FastAPI event loop.
        tasks.extend(
            [
                asyncio.to_thread(lookup_netease, query, "", 0),
                asyncio.to_thread(lookup_sites, query, "", search_web),
            ]
        )
        more = await asyncio.gather(*tasks, return_exceptions=True)
        groups["LRCLIB variants"] = [
            c
            for batch in more[: len(variants)]
            if isinstance(batch, list)
            for c in batch
        ]
        for output in more[len(variants) :]:
            if not isinstance(output, tuple):
                continue
            for candidate in _external_candidates(output[0], query):
                group = candidate["source"]
                if group in ("网易云音乐", "UtaTen", "J-Lyric"):
                    groups.setdefault(group, []).append(candidate)

    # One result per source group first, then fill remaining slots. Otherwise
    # VCPedia often crowds out every independent candidate in the 6-item UI.
    results = []

    def add(candidate):
        c = valid(candidate)
        if c is None:
            return False
        key = (c["title"], c["text"])
        if key in seen:
            return False
        seen.add(key)
        results.append(c)
        return True

    for group in groups.values():
        for candidate in group:
            if add(candidate):
                break
    for group in groups.values():
        for candidate in group:
            if len(results) >= 6:
                break
            add(candidate)
    return results[:6]
