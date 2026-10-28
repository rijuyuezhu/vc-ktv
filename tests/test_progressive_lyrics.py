"""Fast independent lyric phases still use authenticated source-bound tickets."""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.lyric_selection import search_lyric_candidates

api_module = importlib.import_module("vc_ktv.app")


def test_fast_provider_never_waits_for_slow_external_sources(monkeypatch):
    from vc_ktv import lyric_selection as source

    async def fast_lrclib(query):
        return [dict(title=query, text="第一句歌词\n第二句歌词", source="LRCLIB")]

    async def blocked_wiki(query):
        await asyncio.sleep(10)
        raise AssertionError("VCPedia must not run")

    monkeypatch.setattr(source, "_lrclib", fast_lrclib)
    monkeypatch.setattr(source, "vcpedia_lyric_candidates", blocked_wiki)
    monkeypatch.setattr(
        source, "lookup_netease", lambda *args: pytest.fail("No Netease")
    )
    monkeypatch.setattr(source, "lookup_sites", lambda *args: pytest.fail("No sites"))
    result = asyncio.run(
        asyncio.wait_for(search_lyric_candidates("千本桜", phase="lrclib"), 0.4)
    )
    assert len(result) == 1 and result[0]["source"] == "LRCLIB"


def test_optional_more_sources_are_not_in_fast_vcpedia_search(monkeypatch):
    from vc_ktv import lyric_selection as source

    async def fast(query):
        return [dict(title=query, text="第一句\n第二句", source="VCPedia")]

    monkeypatch.setattr(source, "vcpedia_lyric_candidates", fast)
    monkeypatch.setattr(
        source, "lookup_netease", lambda *args: pytest.fail("No fallback")
    )
    monkeypatch.setattr(
        source, "lookup_sites", lambda *args: pytest.fail("No fallback")
    )
    result = asyncio.run(search_lyric_candidates("千本樱", phase="vcpedia"))
    assert result[0]["source"] == "VCPedia"


def test_phase_tickets_sign_and_owner_isolation(tmp_path, monkeypatch):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"partial-test-key",
        auto_prepare=False,
    )
    app.state.events.create("搜索", "123456")

    async def metadata(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "【洛天依原创】《笼中少女》【PV】",
            "uploader": "UP",
            "cover": "",
            "duration": 40,
        }

    async def phases(query, phase="all"):
        assert query == "笼中少女"
        return [
            dict(
                title=query,
                source={
                    "lrclib": "LRCLIB",
                    "vcpedia": "VCPedia",
                    "more": "网易云音乐",
                }.get(phase, "LRCLIB"),
                text="这是歌词第一行\n这是歌词第二行",
            )
        ]

    app.state.lookup = metadata
    monkeypatch.setattr(api_module, "search_lyric_candidates", phases)
    with TestClient(app) as owner, TestClient(app) as other:
        join_and_register(owner, "owner")
        join_and_register(other, "other")
        requested = owner.post("/api/songs", json={"bvid": "BV0000000001"}).json()
        song = requested["id"]
        assert requested["lyric_query"] == "笼中少女"
        for phase in ("vcpedia", "lrclib", "more"):
            url = f"/api/songs/{song}/lyrics/candidates?phase={phase}"
            assert other.get(url).status_code in (401, 404)
            response = owner.get(url)
            assert response.status_code == 200, response.text
            candidate = response.json()["candidates"][0]
            assert response.json()["query"] == "笼中少女"
            assert candidate["ticket"]
            assert (
                owner.post(
                    f"/api/songs/{song}/lyrics/select",
                    json={
                        "ticket": candidate["ticket"],
                        "text": candidate["text"],
                        "source": "LRCLIB"
                        if candidate["source"] != "LRCLIB"
                        else "VCPedia",
                    },
                ).status_code
                == 403
            )
        assert (
            owner.post(
                f"/api/songs/{song}/lyrics/select",
                json={
                    "ticket": candidate["ticket"],
                    "text": candidate["text"],
                    "source": candidate["source"],
                },
            ).status_code
            == 200
        )
        state = owner.get("/api/state").json()
        assert state["waiting"][0]["lyric_query"] == "笼中少女"


def test_invalid_phase_and_retrieval_rate_limit(tmp_path):
    app = create_app(
        root=tmp_path / "root",
        password="admin",
        secret=b"test123",
        auto_prepare=False,
    )
    app.state.events.create("搜索", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "测试歌曲",
            "uploader": "UP",
            "cover": "",
            "duration": 40,
        }

    async def no_results(query):
        return []

    app.state.lookup = lookup
    app.state.candidate_search = no_results
    with TestClient(app) as owner:
        join_and_register(owner, "owner")
        sid = owner.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        path = f"/api/songs/{sid}/lyrics/candidates"
        assert owner.get(path + "?phase=untrusted").status_code == 422
        for _ in range(30):
            assert owner.get(path).status_code == 200
        assert owner.get(path).status_code == 429
