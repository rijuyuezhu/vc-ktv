"""NetEase song-specific metadata and audio-to-video source download."""

from pathlib import Path
from typing import Any, Mapping

from .base import DownloadTools, SongInfo


class NetEaseSource:
    kind = "netease"

    def metadata(self, info: Mapping[str, Any], *, ref: str, page: int = 1) -> SongInfo:
        return SongInfo(
            kind=self.kind,
            source_id=str(ref),
            bvid="",
            page=1,
            title=info["title"],
            uploader=info["uploader"],
            cover=info["cover"],
            duration=info["duration"],
        )

    def download(
        self, song: Mapping[str, Any], directory: Path, tools: DownloadTools
    ) -> Path:
        return tools.music(
            int(song["source_id"]),
            song["cover"],
            directory,
            duration=song["duration"],
            cookies=tools.cookies(),
        )
