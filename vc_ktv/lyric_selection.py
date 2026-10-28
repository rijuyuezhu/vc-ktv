"""Bounded lyric candidates for the submitter's selection."""

from __future__ import annotations

import asyncio
import re

import httpx

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


async def search_lyric_candidates(query: str) -> list[dict]:
    """Show plausible choices, never silently choose the winner."""
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
    results = []
    seen = set()
    for variants in wiki:
        if isinstance(variants, BaseException):
            continue
        for candidate in variants:
            if not isinstance(candidate.get("text"), str):
                continue
            text = candidate["text"].strip()[:12_000]
            key = (candidate["title"], text)
            if key in seen or len(text.splitlines()) < 2:
                continue
            seen.add(key)
            results.append(
                {
                    "title": candidate["title"],
                    "source": "VCPedia",
                    "text": text,
                    "url": candidate["url"],
                }
            )
            if len(results) >= 6:
                return results
    if isinstance(sources[1], list):
        for candidate in sources[1]:
            key = (candidate["title"], candidate["text"])
            if key not in seen and len(results) < 6:
                seen.add(key)
                results.append(candidate)
    return results
