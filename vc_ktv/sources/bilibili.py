"""Bilibili song-specific metadata and original-video download."""

from pathlib import Path
from typing import Any, Mapping

from .base import DownloadTools, SongInfo


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
            )
        return tools.video(song["bvid"], song["page"], directory)
