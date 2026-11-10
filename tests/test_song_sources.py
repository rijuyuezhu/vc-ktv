"""Keep the two source adapters interchangeable without changing public APIs."""

import pytest

from vc_ktv.sources import DownloadTools, get_source


def test_bilibili_metadata_matches_existing_song_row():
    source = get_source("bilibili")
    info = {
        "bvid": "BV0000000001",
        "page": 2,
        "title": "Song",
        "uploader": "UP",
        "cover": "https://example.com/a.png",
        "duration": 120,
    }
    metadata = source.metadata(info, ref="BV0000000001", page=2)
    assert metadata.db_values("guest", 123) == (
        "BV0000000001",
        2,
        "Song",
        "UP",
        "https://example.com/a.png",
        120,
        "guest",
        123,
        "bilibili",
        "",
    )


def test_netease_metadata_matches_existing_song_row():
    source = get_source("netease")
    metadata = source.metadata(
        {"title": "Song", "uploader": "Artist", "cover": "", "duration": 181},
        ref="2613869782",
    )
    assert metadata.db_values("guest", 456) == (
        "",
        1,
        "Song",
        "Artist",
        "",
        181,
        "guest",
        456,
        "netease",
        "2613869782",
    )


def test_source_download_injection_preserves_quality_and_cookies(tmp_path):
    calls = []

    def video(*args, **kwargs):
        calls.append(("video", args, kwargs))
        return tmp_path / "bilibili.mp4"

    def music(*args, **kwargs):
        calls.append(("music", args, kwargs))
        return tmp_path / "netease.mp4"

    def cookie():
        calls.append(("cookies",))
        return {"music_u": "fake"}

    context = DownloadTools(
        video=video, music=music, cookies=cookie, video_quality_keyword=True
    )
    bvid = {"bvid": "BV0000000001", "page": 2, "video_quality": 480}
    assert (
        get_source("bilibili").download(bvid, tmp_path, context).name == "bilibili.mp4"
    )
    assert calls == [("video", ("BV0000000001", 2, tmp_path), {"max_height": 480})]
    calls.clear()
    context = DownloadTools(
        video=video, music=music, cookies=cookie, video_quality_keyword=False
    )
    get_source("bilibili").download(bvid, tmp_path, context)
    assert calls == [("video", ("BV0000000001", 2, tmp_path), {})]
    calls.clear()
    context = DownloadTools(
        video=video,
        music=music,
        cookies=cookie,
        video_quality_keyword=True,
        prefer_av1=True,
    )
    get_source("bilibili").download(bvid, tmp_path, context)
    assert calls == [
        (
            "video",
            ("BV0000000001", 2, tmp_path),
            {"max_height": 480, "prefer_av1": True},
        )
    ]
    calls.clear()
    context = DownloadTools(
        video=video,
        music=music,
        cookies=cookie,
        video_quality_keyword=True,
        prefer_h264=True,
    )
    get_source("bilibili").download(bvid, tmp_path, context)
    assert calls == [
        (
            "video",
            ("BV0000000001", 2, tmp_path),
            {"max_height": 480, "prefer_h264": True},
        )
    ]
    calls.clear()
    netease = {"source_id": "2613869782", "cover": "cover", "duration": 180}
    assert (
        get_source("netease").download(netease, tmp_path, context).name == "netease.mp4"
    )
    assert calls == [
        ("cookies",),
        (
            "music",
            (2613869782, "cover", tmp_path),
            {"duration": 180, "cookies": {"music_u": "fake"}},
        ),
    ]


def test_unknown_source_is_explicitly_rejected():
    with pytest.raises(KeyError):
        get_source("unsupported")
