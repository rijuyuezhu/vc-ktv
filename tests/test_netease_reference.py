"""The first music-source slice intentionally has no network dependencies."""

import pytest

from vc_ktv.sources.netease_reference import parse_netease_song_id


@pytest.mark.parametrize(
    ("input_text", "expected"),
    [
        ("1234567", 1234567),
        ("  1234567\n", 1234567),
        ("https://music.163.com/song?id=1234567", 1234567),
        ("https://music.163.com/#/song?id=1234567", 1234567),
        ("https://music.163.com/m/song?id=1234567&userid=999", 1234567),
        ("https://y.music.163.com/m/song?id=1234567", 1234567),
        ("http://music.163.com/song/?id=1234567", 1234567),
    ],
)
def test_official_single_song_references(input_text, expected):
    assert parse_netease_song_id(input_text) == expected


@pytest.mark.parametrize(
    "input_text",
    [
        "",
        "0",
        "-1",
        "１２３",
        "123a",
        str(2**63),
        "https://evil.com/song?id=123",
        "https://music.163.com.evil.com/song?id=123",
        "https://music.163.com@evil.com/song?id=123",
        "https://music.163.com:8443/song?id=123",
        "javascript:alert(1)",
        "https://music.163.com/playlist?id=123",
        "https://music.163.com/#/playlist?id=123",
        "https://music.163.com/song?id=123&id=456",
        "https://music.163.com/song?id=",
        "https://music.163.com/song?id=abc",
        "https://music.163.com/song?id=123#/playlist?id=456",
        "https://163cn.tv/Xxxx",
        "https://music.163.com/song?id=123" + "x" * 2049,
    ],
)
def test_reject_non_song_and_untrusted_references(input_text):
    with pytest.raises(ValueError):
        parse_netease_song_id(input_text)


def test_reject_non_text_values():
    with pytest.raises(ValueError):
        parse_netease_song_id(None)
