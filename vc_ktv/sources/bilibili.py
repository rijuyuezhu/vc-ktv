"""Bilibili song-specific metadata and original-video download."""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from vc_ktv.sources.base import DownloadTools, SongInfo


class BilibiliSource:
    kind = "bilibili"

    def metadata(self, info: Mapping[str, Any], *, ref: str, page: int = 1) -> SongInfo:
        return SongInfo(
            kind=self.kind,
            source_id="",
            bvid=info["bvid"],
            page=info["page"],
            title=info["title"],
            uploader=info["uploader"],
            cover=info["cover"],
            duration=info["duration"],
        )

    def download(
        self, song: Mapping[str, Any], directory: Path, tools: DownloadTools
    ) -> Path:
        if tools.video_quality_keyword:
            return tools.video(
                song["bvid"],
                song["page"],
                directory,
                max_height=song["video_quality"],
                **({"prefer_av1": True} if tools.prefer_av1 else {}),
                **({"prefer_h264": True} if tools.prefer_h264 else {}),
            )
        return tools.video(song["bvid"], song["page"], directory)


async def bilibili_lookup(bvid: str, page: int) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 VC-KTV/0.1",
        "Referer": "https://www.bilibili.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            res = await client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=headers,
            )
            res.raise_for_status()
            envelope = res.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(
            status_code=502, detail=f"B 站视频信息暂时不可用：{type(e).__name__}"
        ) from None
    if envelope.get("code") != 0 or not envelope.get("data"):
        raise HTTPException(
            status_code=404, detail="B 站未找到该视频，可能已删除或需要登录"
        )
    d = envelope["data"]
    pages = d.get("pages") or [
        {"page": 1, "part": d.get("title", ""), "duration": d.get("duration", 0)}
    ]
    if not 1 <= page <= len(pages):
        raise HTTPException(status_code=400, detail=f"此视频只有 {len(pages)} P")
    p = pages[page - 1]
    duration = int(p.get("duration") or d.get("duration") or 0)
    if duration <= 0:
        raise HTTPException(status_code=400, detail="视频时长无效")
    return {
        "bvid": d.get("bvid", bvid),
        "page": page,
        "title": d.get("title", "未命名视频")[:200],
        "part": p.get("part", ""),
        "pages": [
            {"page": q.get("page", i + 1), "part": q.get("part", "")}
            for i, q in enumerate(pages)
        ],
        "uploader": d.get("owner", {}).get("name", "未知 UP 主")[:100],
        "cover": re.sub(
            r"^http://([a-zA-Z0-9.-]+\.hdslb\.com)/",
            r"https://\1/",
            d.get("pic", ""),
        ),
        "duration": duration,
        "official_url": f"https://www.bilibili.com/video/{quote(bvid)}/?p={page}",
        "embed_url": f"https://player.bilibili.com/player.html?bvid={quote(bvid)}&page={page}&high_quality=1",
    }
