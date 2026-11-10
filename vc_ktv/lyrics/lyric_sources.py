"""Read-only extraction of *candidates*, never silently trusted as song lyrics."""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag


async def vcpedia_lyric_candidates(title: str) -> list[dict]:
    """Read the MediaWiki rendered lyric section and return editable variants."""
    title = title.strip()
    if not title or len(title) > 100 or any(c in title for c in "\r\n\x00"):
        raise ValueError("无效的 VCPedia 条目名称")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://vcpedia.cn/api.php",
            params={"action": "parse", "page": title, "prop": "text", "format": "json"},
            headers={"User-Agent": "VC-KTV/0.2 (read-only lyrics candidate lookup)"},
        )
        response.raise_for_status()
    data = response.json()
    if "error" in data:
        return []
    markup = data.get("parse", {}).get("text", {}).get("*", "")
    if not markup:
        return []
    soup = BeautifulSoup(markup, "html.parser")
    headings = []
    for h in soup.find_all(["h2", "h3"]):
        if not isinstance(h, Tag):
            continue
        headline = h.find(class_="mw-headline")
        if headline is not None and "歌词" in headline.get_text():
            headings.append(h)
    variants = []
    for heading in headings:
        for element in heading.next_siblings:
            if isinstance(element, Tag) and element.name in ("h2", "h3"):
                break
            if not isinstance(element, Tag):
                continue
            poems = (
                [element] if "poem" in (element.get("class") or ()) else []
            ) + element.select("div.poem")
            for poem in poems:
                # Each poem is a separate candidate. Keep inline spans together while
                # respecting line breaks, instead of flattening it into one paragraph.
                cloned = BeautifulSoup(str(poem), "html.parser")
                for br in cloned.select("br"):
                    br.replace_with("\n")
                for tag in cloned.select("script,style,sup,.reference"):
                    tag.decompose()
                lines = []
                for line in cloned.get_text().splitlines():
                    line = re.sub(r"[\t\u200b\u3000 ]+", " ", line).strip()
                    # Consecutive identical lines can be intentional repeats.
                    if line:
                        lines.append(line)
                value = "\n".join(lines)
                if len(lines) >= 2 and value not in [x["text"] for x in variants]:
                    variants.append(
                        {
                            "title": title,
                            "text": value,
                            "source": "VCPedia",
                            "url": "https://vcpedia.cn/"
                            + quote(title.replace(" ", "_")),
                            "note": "仅供核对，请检查翻唱、改词和省略段落；此来源不提供逐字时间戳",
                        }
                    )
    return variants


async def vcpedia_find(query: str):
    if not query.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://vcpedia.cn/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query[:80],
                    "srlimit": 5,
                    "format": "json",
                },
                headers={"User-Agent": "VC-KTV/0.1 (song metadata lookup)"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            {
                "title": entry["title"],
                "url": "https://vcpedia.cn/" + quote(entry["title"].replace(" ", "_")),
            }
            for entry in data.get("query", {}).get("search", [])
        ]
    except (httpx.HTTPError, ValueError, KeyError):
        return []
