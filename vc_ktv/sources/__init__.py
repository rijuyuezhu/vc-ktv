"""Fixed, explicit source registry. Lyrics are independent of media sources."""

from vc_ktv.sources.base import DownloadTools, SongInfo, SongSource
from vc_ktv.sources.bilibili import BilibiliSource
from vc_ktv.sources.netease import NetEaseSource

SOURCES: dict[str, SongSource] = {
    "bilibili": BilibiliSource(),
    "netease": NetEaseSource(),
}


def get_source(kind: str) -> SongSource:
    return SOURCES[kind]


__all__ = ["DownloadTools", "SongInfo", "SongSource", "get_source"]
