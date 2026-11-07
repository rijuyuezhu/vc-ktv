"""Source route modularization must preserve HTTP contracts and dependency injection."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app


def test_source_routes_and_existing_dependency_injection(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="global-test-password",
        secret=b"source-api-boundary",
        auto_prepare=False,
    )
    app.state.events.create("Room", "123456")
    seen = []

    async def bilibili_lookup(bvid, page):
        seen.append(("bilibili", bvid, page))
        return {
            "bvid": bvid,
            "page": page,
            "title": "B Song",
            "uploader": "Uploader",
            "cover": "",
            "duration": 100,
            "pages": [{"page": page, "part": "P"}],
        }

    async def netease_resolve(ref):
        seen.append(("netease_resolve", ref))
        return 42

    async def netease_lookup(song_id):
        seen.append(("netease_lookup", song_id))
        return {
            "title": "N Song",
            "uploader": "Artist",
            "cover": "",
            "duration": 120,
            "source": "netease",
            "source_id": str(song_id),
        }

    async def netease_search(query, *, page=0):
        seen.append(("netease_search", query, page))
        return [{"id": 42, "name": "N Song"}]

    app.state.lookup = bilibili_lookup
    app.state.netease_resolve = netease_resolve
    app.state.netease_lookup = netease_lookup
    app.state.netease_search = netease_search

    contract = app.openapi()["paths"]
    assert {
        ("/api/preview", "get"),
        ("/api/netease/search", "get"),
        ("/api/netease/preview", "get"),
        ("/api/songs", "post"),
        ("/api/netease/songs", "post"),
    } <= {(path, method) for path, ops in contract.items() for method in ops}

    with TestClient(app) as guest, TestClient(app) as outsider:
        assert (
            outsider.get("/api/preview", params={"bvid": "BV0000000001"}).status_code
            == 401
        )
        assert (
            outsider.post("/api/songs", json={"bvid": "BV0000000001"}).status_code
            == 401
        )
        assert (
            outsider.get("/api/netease/search", params={"q": "N Song"}).status_code
            == 401
        )
        assert (
            outsider.post("/api/netease/songs", json={"ref": "42"}).status_code == 401
        )
        join_and_register(guest, "Singer")

        assert (
            guest.get("/api/preview", params={"bvid": "BV0000000001"}).json()["title"]
            == "B Song"
        )
        assert guest.get(
            "/api/netease/search", params={"q": "N Song", "page": 2}
        ).json()["songs"] == [{"id": 42, "name": "N Song"}]
        assert (
            guest.get("/api/netease/preview", params={"ref": "42"}).json()["title"]
            == "N Song"
        )
        b_song = guest.post("/api/songs", json={"bvid": "BV0000000001"})
        assert b_song.status_code == 200, b_song.text
        n_song = guest.post("/api/netease/songs", json={"ref": "42"})
        assert n_song.status_code == 200, n_song.text
        assert "source" not in b_song.json()
        assert n_song.json()["source"] == "netease"

        with app.state.store.lock:
            rows = [
                tuple(row)
                for row in app.state.store.db.execute(
                    "SELECT id,source_type,source_id,bvid FROM songs ORDER BY id"
                )
            ]
        assert rows == [
            (b_song.json()["id"], "bilibili", "", "BV0000000001"),
            (n_song.json()["id"], "netease", "42", ""),
        ]
        assert ("netease_search", "N Song", 2) in seen
        assert ("bilibili", "BV0000000001", 1) in seen
        assert ("netease_lookup", 42) in seen
