"""Upstream source adapters and guest-facing candidate coverage, all offline."""

import asyncio

import httpx

from ktv_app.lyric_names import title_variants
from vc_ktv import lyric_selection as lyrics


def track(title, source, text, artist="初音ミク", identifier="123"):
    return {
        "id": identifier,
        "trackName": title,
        "artistName": artist,
        "provider": source,
        "source": "https://music.163.com/song?id=123",
        "syncedLyrics": text,
    }


def set_sources(monkeypatch, wiki, lrclib, netease, pages, *, query_titles=None):
    async def names(query):
        return query_titles if query_titles is not None else []

    async def wiki_results(title):
        return wiki

    async def lrclib_results(title):
        return lrclib.get(title, [])

    monkeypatch.setattr(lyrics, "_wiki_titles", names)
    monkeypatch.setattr(lyrics, "_lrclib", lrclib_results)
    monkeypatch.setattr(lyrics, "vcpedia_lyric_candidates", wiki_results)

    async def netease_results(query):
        return [
            candidate
            for item in netease
            if (
                candidate := lyrics._candidate(
                    item["trackName"],
                    "网易云音乐",
                    item["syncedLyrics"],
                    item["source"],
                    item["artistName"],
                )
            )
        ]

    monkeypatch.setattr(lyrics, "_netease", netease_results)


def test_conservative_japanese_script_variants_change_only_search_title():
    assert title_variants("千本樱") == ["千本櫻", "千本桜"]


def test_independent_sources_get_visible_slots(monkeypatch):
    wikis = [
        dict(title="千本樱", source="VCPedia", text=f"Wiki {i}\n完整歌词 {i}")
        for i in range(4)
    ]
    lr = {"千本樱": [dict(title="千本桜", source="LRCLIB", text="原文一\n原文二")]}
    net = [track("千本桜", "网易云音乐", "[00:01]さくら\n[00:02]さくら")]
    set_sources(monkeypatch, wikis, lr, net, [])
    candidates = asyncio.run(lyrics.search_lyric_candidates("千本樱"))
    assert len(candidates) == 6
    assert {c["source"] for c in candidates} >= {
        "VCPedia",
        "LRCLIB",
        "网易云音乐",
    }
    by_source = {c["source"]: c for c in candidates}
    assert by_source["网易云音乐"]["text"] == "さくら\nさくら"
    assert by_source["网易云音乐"]["artist"] == "初音ミク"


def test_bounded_fallback_when_primary_already_has_six(monkeypatch):
    wikis = [
        dict(title=f"第{i}版", source="VCPedia", text=f"第{i}版\n第{i}版")
        for i in range(6)
    ]
    set_sources(
        monkeypatch,
        wikis,
        {"测试歌曲": [dict(title="原文", source="LRCLIB", text="一行\n二行")]},
        [],
        [],
    )
    results = asyncio.run(lyrics.search_lyric_candidates("测试歌曲"))
    assert len(results) == 7  # Every available source contributes independently.


def test_single_source_six_results_still_queries_independent_providers(monkeypatch):
    same_source = {
        "千本桜": [
            dict(title=f"千本桜 第{i}版", source="LRCLIB", text=f"候选 {i}\n歌词 {i}")
            for i in range(6)
        ]
    }
    net = [track("千本桜", "网易云音乐", "歌曲第一句\n歌曲第二句")]
    set_sources(monkeypatch, [], same_source, net, [])
    results = asyncio.run(lyrics.search_lyric_candidates("千本桜"))
    assert len(results) == 7
    assert "网易云音乐" in {x["source"] for x in results}


def test_bad_independent_provider_does_not_fail_guest_search(monkeypatch):
    async def names(query):
        raise httpx.ConnectError("vcpedia unavailable")

    async def lrclib(query):
        return [dict(title="测试歌曲", source="LRCLIB", text="第一句歌词\n第二句歌词")]

    monkeypatch.setattr(lyrics, "_wiki_titles", names)
    monkeypatch.setattr(lyrics, "_lrclib", lrclib)

    async def failed_wiki(query):
        raise httpx.ConnectError("wiki was unavailable")

    monkeypatch.setattr(lyrics, "vcpedia_lyric_candidates", failed_wiki)

    async def failed_netease(query):
        raise httpx.ConnectError("netease down")

    monkeypatch.setattr(lyrics, "_netease", failed_netease)
    result = asyncio.run(lyrics.search_lyric_candidates("测试歌曲"))
    assert len(result) == 1 and result[0]["source"] == "LRCLIB"


def test_unrelated_lyrics_are_collected_but_one_line_is_unusable():
    assert lyrics._candidate(
        "完全不同", "网易云音乐", "原文一\n原文二", "https://music.163.com/song?id=123"
    ) == {
        "title": "完全不同",
        "source": "网易云音乐",
        "url": "https://music.163.com/song?id=123",
        "text": "原文一\n原文二",
    }
    assert (
        lyrics._candidate(
            "千本桜", "网易云音乐", "只有一句", "https://music.163.com/song?id=123"
        )
        is None
    )


def test_guest_never_auto_selects_lyric_from_search(monkeypatch, tmp_path):
    # A guest-facing suggestion is not a commit to any song or user.
    w = [dict(title="千本樱", source="VCPedia", text="第一句\n第二句")]
    set_sources(
        monkeypatch, w, {}, [track("千本桜", "网易云音乐", "第三句\n第四句")], []
    )
    results = asyncio.run(lyrics.search_lyric_candidates("千本樱"))
    assert len(results) == 2
    assert all("ticket" not in c for c in results)
