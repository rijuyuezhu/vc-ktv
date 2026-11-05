"""Name-only query variants. These conversions never rewrite lyric content."""

import re
from functools import lru_cache

# Conservative, unambiguous Japanese shinjitai variants used in song/artist names.
# OpenCC's Python distribution ships Chinese conversions, but no t2jp dictionary.
_TRADITIONAL = "櫻黑澤樂廣戰聲國龍圓氣戀"
_JAPANESE = "桜黒沢楽広戦声国竜円気恋"
_TO_JAPANESE = str.maketrans(_TRADITIONAL, _JAPANESE)
_FROM_JAPANESE = str.maketrans(_JAPANESE, _TRADITIONAL)


@lru_cache(maxsize=2)
def chinese_converter(conversion):
    from opencc import OpenCC

    return OpenCC(conversion)


def title_variants(song):
    if not re.search(r"[\u3400-\u9fff]", song):
        return []
    traditional = chinese_converter("s2t").convert(song.translate(_FROM_JAPANESE))
    variants = [
        traditional,
        chinese_converter("t2s").convert(traditional),
        traditional.translate(_TO_JAPANESE),
    ]
    return list(dict.fromkeys(v for v in variants if v != song))[:3]
