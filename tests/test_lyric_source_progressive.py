"""Independent provider behavior: collection only, without title/artist ranking."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv import lyric_selection
from vc_ktv.app import create_app


@pytest.mark.parametrize("query", ["花", "秋鶄岚夜曲"])
def test_lyric_phase_accepts_single_character_and_returns_signed_candidates(
    tmp_path, monkeypatch, query
):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"unit-test-key",
        auto_prepare=False,
    )
    app.state.events.create("测试", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": query,
            "uploader": "UP",
            "cover": "",
            "duration": 60,
        }

    async def wiki(title):
        return [
            {
                "title": title,
                "text": "这是第一句歌词\n这是第二句歌词",
                "source": "VCPedia",
                "url": "https://vcpedia.cn",
            }
        ]

    app.state.lookup = lookup
    monkeypatch.setattr(lyric_selection, "vcpedia_lyric_candidates", wiki)
    with TestClient(app) as guest:
        join_and_register(guest, "Tester")
        song = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        result = guest.get(
            f"/api/songs/{song}/lyrics/candidates",
            params={"phase": "vcpedia", "query": query},
        )
        assert result.status_code == 200, result.text
        candidate = result.json()["candidates"][0]
        assert candidate["title"] == query and candidate["ticket"]
        assert (
            guest.post(
                f"/api/songs/{song}/lyrics/select",
                json={
                    "source": candidate["source"],
                    "text": candidate["text"],
                    "ticket": candidate["ticket"],
                },
            ).status_code
            == 200
        )


def test_wiki_search_returns_exact_source_titles_without_match_filters(monkeypatch):
    async def pages(query):
        assert query == "秋鶄岚夜曲"
        return ["秋䴖岚夜曲", "四万秋"]

    async def poem(title):
        return [
            {
                "title": title,
                "text": "第一句歌词可以使用\n第二句歌词可以使用",
                "source": "VCPedia",
                "url": "https://vcpedia.cn",
            }
        ]

    monkeypatch.setattr(lyric_selection, "_wiki_titles", pages)
    monkeypatch.setattr(lyric_selection, "vcpedia_lyric_candidates", poem)
    rows = asyncio.run(lyric_selection.search_lyric_candidates("秋鶄岚夜曲", "wiki"))
    assert [r["title"] for r in rows] == ["秋䴖岚夜曲", "四万秋"]


def test_netease_collects_catalogue_candidates_without_title_matching(monkeypatch):
    async def handler(request):
        if request.url.path.endswith("search/get"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "result": {
                        "songs": [
                            {
                                "id": 10,
                                "name": "另一首不相似的歌",
                                "artists": [{"name": "其他歌手"}],
                            },
                            {
                                "id": 11,
                                "name": "秋鶄岚夜曲",
                                "artists": [{"name": "洛天依"}],
                            },
                            {
                                "id": 12,
                                "name": "没有歌词的歌",
                                "artists": [{"name": "无"}],
                            },
                        ]
                    },
                },
            )
        identifier = request.url.params.get("id")
        if identifier == "12":
            return httpx.Response(200, json={"code": 200, "lrc": {"lyric": ""}})
        return httpx.Response(
            200,
            json={"code": 200, "lrc": {"lyric": "[00:01.00]第一句\n[00:05.00]第二句"}},
        )

    original = lyric_selection.httpx.AsyncClient

    def mock_client(**kwargs):
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(lyric_selection.httpx, "AsyncClient", mock_client)
    rows = asyncio.run(lyric_selection.search_lyric_candidates("秋鶄岚夜曲", "netease"))
    assert [r["title"] for r in rows] == ["另一首不相似的歌", "秋鶄岚夜曲"]
    assert rows[0]["artist"] == "其他歌手"
    assert rows[0]["text"] == "第一句\n第二句"


def test_slow_source_never_blocks_another_phase(monkeypatch):
    async def slow(query):
        await asyncio.sleep(30)
        return []

    async def fast(query):
        return [{"title": query, "text": "第一句\n第二句", "source": "LRCLIB"}]

    monkeypatch.setattr(lyric_selection, "vcpedia_lyric_candidates", slow)
    monkeypatch.setattr(lyric_selection, "_lrclib", fast)
    monkeypatch.setattr(lyric_selection, "DEADLINE", 0.1)

    async def run():
        pending = asyncio.create_task(
            lyric_selection.search_lyric_candidates("花", "vcpedia")
        )
        ready = await asyncio.wait_for(
            lyric_selection.search_lyric_candidates("花", "lrclib"), timeout=0.05
        )
        assert ready[0]["title"] == "花"
        assert await pending == []

    asyncio.run(run())


def test_more_only_uses_variant_lrclib_and_no_web_scraping(monkeypatch):
    monkeypatch.setattr(lyric_selection, "title_variants", lambda q: ["秋鶄嵐夜曲"])
    queried = []

    async def lrclib(query):
        queried.append(query)
        return [{"title": query, "source": "LRCLIB", "text": "正文第一行\n正文第二行"}]

    monkeypatch.setattr(lyric_selection, "_lrclib", lrclib)
    results = asyncio.run(lyric_selection.search_lyric_candidates("秋鶄岚夜曲", "more"))
    assert [r["title"] for r in results] == ["秋鶄嵐夜曲"]
    assert queried == ["秋鶄嵐夜曲"]


def test_slow_wiki_article_does_not_discard_a_ready_article(monkeypatch):
    async def pages(query):
        return ["秋䴖岚夜曲", "slow-article"]

    async def article(title):
        if title == "slow-article":
            await asyncio.sleep(10)
        return [{"title": title, "source": "VCPedia", "text": "歌词第一句\n歌词第二句"}]

    monkeypatch.setattr(lyric_selection, "_wiki_titles", pages)
    monkeypatch.setattr(lyric_selection, "vcpedia_lyric_candidates", article)
    # Keep the production timeout for this assertion: one straggler must not
    # discard a completed candidate or cause a 15-second stall.
    rows = asyncio.run(lyric_selection.search_lyric_candidates("秋鶄岚夜曲", "wiki"))
    assert [r["title"] for r in rows] == ["秋䴖岚夜曲"]
