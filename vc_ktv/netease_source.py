"""Pure validation of NetEase single-song references (no network or credentials)."""

from urllib.parse import parse_qs, urlsplit

ALLOWED_HOSTS = frozenset({"music.163.com", "y.music.163.com"})
ALLOWED_PATHS = frozenset({"/song", "/m/song"})
MAX_SONG_ID = 2**63 - 1


def _song_id(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("请输入网易云单曲 ID 或官方单曲链接")
    if len(value) > 19:
        raise ValueError("网易云歌曲 ID 无效")
    result = int(value)
    if result < 1 or result > MAX_SONG_ID:
        raise ValueError("网易云歌曲 ID 无效")
    return result


def parse_netease_song_id(value: str) -> int:
    """Accept a numeric ID or an official song URL, never arbitrary fetch targets.

    Short share URLs and playlists are deliberately excluded until a separate
    redirect-resolution design is tested. The result is a numeric ID only.
    """
    if not isinstance(value, str) or not (raw := value.strip()) or len(raw) > 2048:
        raise ValueError("请输入网易云单曲 ID 或官方单曲链接")
    if raw.isascii() and raw.isdecimal():
        return _song_id(raw)
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
    ):
        raise ValueError("仅支持网易云官方单曲链接")
    if parsed.fragment:
        if not parsed.fragment.startswith("/song?"):
            raise ValueError("仅支持网易云官方单曲链接")
        path, query = parsed.fragment.split("?", 1)
        if parsed.path not in {"", "/"} or parsed.query:
            raise ValueError("仅支持网易云官方单曲链接")
    else:
        path, query = parsed.path.rstrip("/"), parsed.query
    if path not in ALLOWED_PATHS:
        raise ValueError("仅支持网易云官方单曲链接")
    ids = parse_qs(query, keep_blank_values=True).get("id", [])
    if len(ids) != 1:
        raise ValueError("网易云单曲链接缺少唯一歌曲 ID")
    return _song_id(ids[0])
