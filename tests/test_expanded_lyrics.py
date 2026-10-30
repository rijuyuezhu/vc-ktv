"""Upstream source adapters and guest-facing candidate coverage, all offline."""

import asyncio

import httpx
import pytest

from ktv_app.lyric_names import similarity, title_variants
from ktv_app.lyric_sources import LyricPage, fetch_page, supported_page
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
    assert similarity("千本樱", "千本桜") == 1


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


def test_utaten_keeps_body_and_repeated_lines_not_ruby_or_romaji():
    page = LyricPage("UtaTen")
    page.feed(
        '<h2 class="newLyricTitle__main">千本桜</h2>'
        '<dt class="newLyricWork__name"><h3>黒うさP</h3></dt>'
        '<div class="hiragana">桜<span class="rt">さくら</span>だ<br>'
        "君が歌う<br>桜だ<br>最後の歌</div>"
        '<div class="romaji">romaji line</div><p>広告</p>'
    )
    row = page.record("https://utaten.com/lyric/test/")
    assert row["plainLyrics"].splitlines() == ["桜だ", "君が歌う", "桜だ", "最後の歌"]


def test_jlyric_requires_real_body():
    page = LyricPage("J-Lyric")
    page.feed(
        '<meta name="description" content="歌手の「曲名」歌詞ページ。">'
        '<p id="Lyric">第一句<br>第二句</p><p>広告文</p>'
    )
    row = page.record("https://j-lyric.net/artist/a123/l456.html")
    assert row["plainLyrics"] == "第一句\n第二句"
    assert row["trackName"] == "曲名"


@pytest.mark.parametrize(
    "url",
    [
        "http://utaten.com/lyric/test/",
        "https://utaten.com.evil.invalid/lyric/test/",
        "https://utaten.com@127.0.0.1/lyric/test/",
        "https://utaten.com:8778/lyric/test/",
        "https://j-lyric.net/not-a-lyric",
        "https://127.0.0.1/lyric/test/",
    ],
)
def test_only_fixed_sites_are_accepted(url):
    assert supported_page(url) is None


def test_lyric_site_fetch_rejects_redirects_and_oversized_bodies():
    for status, headers, body in [
        (302, {"location": "http://127.0.0.1/"}, b""),
        (200, {"content-type": "text/html"}, b"x" * 600001),
    ]:
        transport = httpx.MockTransport(
            lambda req: httpx.Response(status, headers=headers, content=body)
        )
        with httpx.Client(transport=transport) as client:
            with pytest.raises((httpx.HTTPError, ValueError)):
                fetch_page(client, "https://utaten.com/lyric/test/")


def test_guest_never_auto_selects_lyric_from_search(monkeypatch, tmp_path):
    # A guest-facing suggestion is not a commit to any song or user.
    w = [dict(title="千本樱", source="VCPedia", text="第一句\n第二句")]
    set_sources(
        monkeypatch, w, {}, [track("千本桜", "网易云音乐", "第三句\n第四句")], []
    )
    results = asyncio.run(lyrics.search_lyric_candidates("千本樱"))
    assert len(results) == 2
    assert all("ticket" not in c for c in results)


def test_jlyric_search_fallback_fetches_fixed_source_body(monkeypatch):
    from ktv_app import lyric_sources

    requests = []

    def fake_page(client, url, params=None):
        requests.append(url)
        if url == "https://utaten.com/search":
            return "<html><p>没有本曲</p></html>"
        if url == "https://j-lyric.net/artist/a123/l456.html":
            return (
                '<meta name="description" '
                'content="初音ミクの「千本桜」歌詞ページ。">'
                '<p id="Lyric">第一句歌詞<br>第二句歌詞</p>'
            )
        raise AssertionError("Unexpected external URL")

    monkeypatch.setattr(lyric_sources, "fetch_page", fake_page)
    rows, attempts = lyric_sources.lookup_sites(
        "千本樱",
        "",
        lambda query: [
            {"url": "https://j-lyric.net/artist/a123/l456.html"},
            {"url": "https://127.0.0.1/admin"},
        ],
    )
    assert len(rows) == 1
    assert rows[0]["provider"] == "J-Lyric"
    assert rows[0]["plainLyrics"] == "第一句歌詞\n第二句歌詞"
    assert requests == [
        "https://utaten.com/search",
        "https://j-lyric.net/artist/a123/l456.html",
    ]
    assert any(a.get("candidates") == 1 for a in attempts)
