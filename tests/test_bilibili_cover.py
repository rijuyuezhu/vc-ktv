"""Bilibili covers are stored in HTTPS form even when upstream sends HTTP."""

import asyncio
import importlib

import httpx
import pytest

module = importlib.import_module("vc_ktv.app")


@pytest.mark.parametrize(
    "cover,expected",
    [
        (
            "http://i1.hdslb.com/bfs/archive/abc.jpg",
            "https://i1.hdslb.com/bfs/archive/abc.jpg",
        ),
        (
            "https://i2.hdslb.com/bfs/archive/a.jpg",
            "https://i2.hdslb.com/bfs/archive/a.jpg",
        ),
        ("http://example.com/cover.jpg", "http://example.com/cover.jpg"),
    ],
)
def test_bilibili_lookup_cover_url(monkeypatch, cover, expected):
    def respond(request):
        assert request.url.path == "/x/web-interface/view"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "bvid": "BV0000000001",
                    "title": "Song",
                    "duration": 100,
                    "owner": {"name": "UP"},
                    "pic": cover,
                },
            },
        )

    original_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", mock_client)
    result = asyncio.run(module.bilibili_lookup("BV0000000001", 1))
    assert result["cover"] == expected
