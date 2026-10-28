import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv import lyric_selection
from vc_ktv.app import create_app


def app_for_lyrics(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"guest-lyric-key",
        auto_prepare=False,
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "给你的《测试歌曲》。【虚拟歌手原创】",
            "uploader": "someone",
            "cover": "",
            "duration": 281,
        }

    queries = []

    async def search(query):
        queries.append(query)
        return [
            {
                "title": "测试歌曲",
                "source": "VCPedia",
                "url": "https://vcpedia.cn/测试歌曲",
                "text": "第一句歌词很长很长\n第二句歌词很长很长",
            },
            {
                "title": "测试歌曲 (另一版)",
                "source": "LRCLIB",
                "url": "https://lrclib.net/",
                "text": "另一个版本的开场\n另一个版本的副歌",
            },
        ]

    app.state.lookup = lookup
    app.state.candidate_search = search
    return app, queries


def test_guest_chooses_and_searches_with_custom_query(tmp_path):
    app, queries = app_for_lyrics(tmp_path)
    with TestClient(app) as own, TestClient(app) as other:
        join_and_register(other, "其他用户")
        join_and_register(own, "自己")
        created = own.post("/api/songs", json={"bvid": "BV0000000002"})
        assert created.status_code == 200
        assert "测试歌曲" in created.json()["title"]
        sid = created.json()["id"]
        url = f"/api/songs/{sid}/lyrics/candidates"
        assert other.get(url).status_code in (401, 404)
        first = own.get(url)
        assert first.status_code == 200
        assert first.json()["query"] == "测试歌曲"
        assert len(first.json()["candidates"]) == 2
        assert queries == ["测试歌曲"]
        second = own.get(url, params={"query": "测试歌曲 合唱歌手"})
        assert second.status_code == 200
        assert queries[-1] == "测试歌曲 合唱歌手"
        ticket = second.json()["candidates"][1]
        selection = {
            "text": ticket["text"],
            "ticket": ticket["ticket"],
            "source": ticket["source"],
        }
        assert other.post(
            f"/api/songs/{sid}/lyrics/select", json=selection
        ).status_code in (401, 404)
        assert (
            own.post(
                f"/api/songs/{sid}/lyrics/select",
                json={**selection, "text": "完全替换了歌词\n仍然有两行文字"},
            ).status_code
            == 403
        )
        ok = own.post(f"/api/songs/{sid}/lyrics/select", json=selection)
        assert ok.status_code == 200, ok.text
        state = own.get("/api/state").json()["waiting"][0]
        assert state["mine"] and state["lyric_selected"]
        assert state["lyric_source"] == "LRCLIB"
        assert not other.get("/api/state").json()["waiting"][0]["mine"]
        assert "lyric_choice" not in state
        with app.state.store.lock:
            stored = app.state.store.db.execute(
                "SELECT lyric_choice,lyric_source FROM songs WHERE id=?", (sid,)
            ).fetchone()
            assert stored["lyric_choice"] == ticket["text"]
        for _ in range(12):
            own.get(url)
        assert own.get(url).status_code == 429


def test_guest_cannot_change_after_playing(tmp_path):
    app, _ = app_for_lyrics(tmp_path)
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "访客")
        sid = guest.post("/api/songs", json={"bvid": "BV0000000002"}).json()["id"]
        candidate = guest.get(f"/api/songs/{sid}/lyrics/candidates").json()[
            "candidates"
        ][0]
        selection = {
            "text": candidate["text"],
            "source": candidate["source"],
            "ticket": candidate["ticket"],
        }
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        app.state.probe = lambda path: 281.0
        assert (
            admin.post(
                f"/api/admin/songs/{sid}/media",
                files={"file": ("source.mp4", b"test video bytes", "video/mp4")},
            ).status_code
            == 200
        )
        assert admin.post("/api/admin/next").json()["active"]["id"] == sid
        assert (
            guest.post(f"/api/songs/{sid}/lyrics/select", json=selection).status_code
            == 409
        )


def test_multiple_wiki_sources_and_manual_query(monkeypatch):
    async def pages(query):
        return ["测试歌曲", "测试歌曲（合唱）"]

    async def lyric(title):
        return [
            {
                "title": title,
                "source": "VCPedia",
                "text": f"这是 {title} 的歌词\n第二行也有歌词",
                "url": "https://vcpedia.cn",
            }
        ]

    async def lrclib(query):
        return []

    monkeypatch.setattr(lyric_selection, "lookup_netease", lambda *args: ([], []))
    monkeypatch.setattr(lyric_selection, "lookup_sites", lambda *args: ([], []))
    monkeypatch.setattr(lyric_selection, "_wiki_titles", pages)
    monkeypatch.setattr(lyric_selection, "_lrclib", lrclib)
    monkeypatch.setattr(lyric_selection, "vcpedia_lyric_candidates", lyric)
    results = asyncio.run(lyric_selection.search_lyric_candidates("测试歌曲"))
    assert [r["title"] for r in results] == ["测试歌曲", "测试歌曲（合唱）"]


def test_select_after_original_video_ready_starts_enhancement(tmp_path):
    import shutil
    import time

    app, _ = app_for_lyrics(tmp_path)
    app.state.store.save_settings("fair", True)  # Test original-media availability.

    def download(_bv, _page, directory):
        target = directory / "source.mp4"
        target.write_bytes(b"original")
        return target

    def enhance(source, lyrics, target, archive, duration):
        assert "第一句" in lyrics and duration == 281
        archive.mkdir(exist_ok=True)
        (archive / "aligned.lrc").write_text("[00:01.00]第一句")
        shutil.copyfile(source, target)
        return {"lines": 1}

    app.state.autoprep.download = download
    app.state.autoprep.probe = lambda path: 281
    app.state.autoprep.enhance = enhance
    # This test explicitly starts the worker; it does not touch real media.
    app.state.autoprep.start()
    try:
        with TestClient(app) as guest:
            join_and_register(guest, "访客")
            song = guest.post("/api/songs", json={"bvid": "BV0000000002"}).json()["id"]
            for _ in range(90):
                state = guest.get("/api/state").json()
                if state["queue"] and state["queue"][0]["prep_state"] == "downloaded":
                    break
                time.sleep(0.05)
            assert state["queue"][0]["prep_state"] == "downloaded"
            candidates = guest.get(f"/api/songs/{song}/lyrics/candidates").json()[
                "candidates"
            ]
            choice = candidates[0]
            assert (
                guest.post(
                    f"/api/songs/{song}/lyrics/select",
                    json={
                        "source": choice["source"],
                        "text": choice["text"],
                        "ticket": choice["ticket"],
                    },
                ).status_code
                == 200
            )
            for _ in range(90):
                state = guest.get("/api/state").json()
                if state["queue"] and state["queue"][0]["prep_state"] == "done":
                    break
                time.sleep(0.05)
            assert state["queue"][0]["prep_state"] == "done", state["queue"]
            assert state["queue"][0]["has_lyrics"]
    finally:
        app.state.autoprep.stop()


@pytest.mark.parametrize("source", ["网易云音乐", "UtaTen", "J-Lyric"])
def test_new_source_requires_original_guest_and_signed_ticket(tmp_path, source):
    app, _ = app_for_lyrics(tmp_path)

    async def candidate_search(query):
        return [
            {
                "title": query,
                "artist": "初音ミク",
                "source": source,
                "url": "https://example.invalid",
                "text": "保留原文的第一行\n原文第二行不更改",
            }
        ]

    app.state.candidate_search = candidate_search
    with TestClient(app) as owner, TestClient(app) as other:
        join_and_register(owner, "点歌者")
        join_and_register(other, "其他用户")
        sid = owner.post("/api/songs", json={"bvid": "BV0000000002"}).json()["id"]
        url = f"/api/songs/{sid}/lyrics/candidates"
        assert other.get(url).status_code in (401, 404)
        candidate = owner.get(url).json()["candidates"][0]
        payload = {
            "source": candidate["source"],
            "text": candidate["text"],
            "ticket": candidate["ticket"],
        }
        assert other.post(
            f"/api/songs/{sid}/lyrics/select", json=payload
        ).status_code in (401, 404)
        assert (
            owner.post(
                f"/api/songs/{sid}/lyrics/select",
                json={**payload, "source": "LRCLIB"},
            ).status_code
            == 403
        )
        ok = owner.post(f"/api/songs/{sid}/lyrics/select", json=payload)
        assert ok.status_code == 200, ok.text
        stored = app.state.store.db.execute(
            "SELECT lyric_choice,lyric_source FROM songs WHERE id=?", (sid,)
        ).fetchone()
        assert stored["lyric_source"] == source
        assert stored["lyric_choice"] == candidate["text"]
