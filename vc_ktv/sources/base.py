"""Only the differences between media platforms belong in SongSource."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class SongInfo:
    kind: str
    source_id: str
    bvid: str
    page: int
    title: str
    uploader: str
    cover: str
    duration: int

    def db_values(self, requester: str, requested_at: int) -> tuple:
        return (
            self.bvid,
            self.page,
            self.title,
            self.uploader,
            self.cover,
            self.duration,
            requester,
            requested_at,
            self.kind,
            self.source_id,
        )


@dataclass(frozen=True)
class DownloadTools:
    video: Callable[..., Path]
    music: Callable[..., Path]
    cookies: Callable[[], Any]
    video_quality_keyword: bool
    prefer_av1: bool = False
    prefer_h264: bool = False


class SongSource(Protocol):
    kind: str

    def metadata(
        self, info: Mapping[str, Any], *, ref: str, page: int = 1
    ) -> SongInfo: ...

    def download(
        self, song: Mapping[str, Any], directory: Path, tools: DownloadTools
    ) -> Path: ...
