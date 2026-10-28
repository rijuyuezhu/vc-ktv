"""Read-only extraction of *candidates*, never silently trusted as song lyrics."""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup


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
    if not markup or len(markup) > 4_000_000:
        return []
    soup = BeautifulSoup(markup, "html.parser")
    headings = [
        h
        for h in soup.find_all(["h2", "h3"])
        if h.find(class_="mw-headline")
        and "歌词" in h.find(class_="mw-headline").get_text()
    ]
    variants = []
    for heading in headings[:4]:
        for element in heading.next_siblings:
            if getattr(element, "name", None) in ("h2", "h3"):
                break
            if not getattr(element, "name", None):
                continue
            poems = (
                [element] if "poem" in element.get("class", []) else []
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
                    if line and (not lines or line != lines[-1]):
                        lines.append(line)
                value = "\n".join(lines)[:30_000]
                if len(value) >= 30 and value not in [x["text"] for x in variants]:
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
    return variants[:5]
