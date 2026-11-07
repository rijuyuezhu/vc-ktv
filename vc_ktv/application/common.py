"""Self-hosted BV-first karaoke with optional local subtitle generation."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse

BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")
MAX_ACTIVE_SONGS = 1000
MAX_USER_SONGS = 20
LRC_LINE = re.compile(
    r"^(?:\[(?:(?:\d{1,2}:)?\d{1,3}:\d{2}(?:[.:]\d{1,3})?)\])+\s*(.*)$"
)
LRC_WORD_TIME = re.compile(r"<(?:(?:\d{1,2}:)?\d{1,3}:\d{2}(?:[.:]\d{1,3})?)>")
STATIC = Path(__file__).resolve().parent.parent.parent / "static"
MEDIA_MIMES = {".mp4": "video/mp4", ".webm": "video/webm", ".m4v": "video/mp4"}


def bail(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


def get_bvid(raw: str) -> str:
    raw = raw.strip()
    # Accept either an exact BV ID or common Bilibili video URLs.
    match = re.search(r"(?:^|/)(BV[0-9A-Za-z]{10})(?:[/?#]|$)", raw)
    v = match.group(1) if match else raw
    if not BVID.fullmatch(v):
        bail(400, "请输入 BV 号或含 BV 号的视频链接")
    return v


def probe_media(file: Path) -> float:
    """Validate a real video file and obtain its actual duration."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(file),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        data = json.loads(proc.stdout)
        streams = data.get("streams", [])
        if not any(
            s.get("codec_type") == "video"
            and 0 < s.get("width", 0) <= 7680
            and 0 < s.get("height", 0) <= 4320
            for s in streams
        ):
            raise ValueError("未找到有效的视频轨道")
        duration = float(data["format"]["duration"])
        if duration <= 0:
            raise ValueError("视频时长必须在 1 秒至 1 小时之间")
        return duration
    except (
        subprocess.SubprocessError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError("视频无效或无法解析，请上传有效的 MP4 或 WebM") from error


class PinnedFileResponse(FileResponse):
    """Keep a media inode alive through FileResponse's deferred open/send."""

    async def __call__(self, scope, receive, send):
        # A pathsend event may be queued for later opening. Stream the file
        # here instead so the pinned link is safe to remove in finally.
        if "http.response.pathsend" in scope.get("extensions", {}):
            scope = {
                **scope,
                "extensions": {
                    key: value
                    for key, value in scope["extensions"].items()
                    if key != "http.response.pathsend"
                },
            }
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


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
        bail(502, f"B 站视频信息暂时不可用：{type(e).__name__}")
    if envelope.get("code") != 0 or not envelope.get("data"):
        bail(404, "B 站未找到该视频，可能已删除或需要登录")
    d = envelope["data"]
    pages = d.get("pages") or [
        {"page": 1, "part": d.get("title", ""), "duration": d.get("duration", 0)}
    ]
    if not 1 <= page <= len(pages):
        bail(400, f"此视频只有 {len(pages)} P")
    p = pages[page - 1]
    duration = int(p.get("duration") or d.get("duration") or 0)
    if duration <= 0:
        bail(400, "视频时长无效")
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
