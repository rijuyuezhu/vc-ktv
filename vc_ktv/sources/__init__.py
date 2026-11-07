"""Fixed, explicit source registry. Lyrics are independent of media sources."""

from .base import DownloadTools, SongInfo, SongSource
from .bilibili import BilibiliSource
from .netease import NetEaseSource

SOURCES: dict[str, SongSource] = {
    "bilibili": BilibiliSource(),
    "netease": NetEaseSource(),
}


def get_source(kind: str) -> SongSource:
    return SOURCES[kind]


__all__ = ["DownloadTools", "SongInfo", "SongSource", "get_source"]
