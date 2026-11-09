"""Bilibili covers are stored in HTTPS form even when upstream sends HTTP."""

import asyncio
import importlib

import httpx
import pytest

module = importlib.import_module("vc_ktv.sources.bilibili")


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


@pytest.mark.parametrize(
    ("data", "page", "status"),
    [
        ({"code": -404}, 1, 404),
        ({"code": 0, "data": {"duration": 100, "title": "Song"}}, 2, 400),
        ({"code": 0, "data": {"duration": 0, "title": "Song"}}, 1, 400),
    ],
)
def test_bilibili_lookup_errors_remain_http_statuses(monkeypatch, data, page, status):
    from fastapi import HTTPException

    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        return original_client(
            *args,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=data)
            ),
            **kwargs,
        )

    monkeypatch.setattr(module.httpx, "AsyncClient", client)
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.bilibili_lookup("BV0000000001", page))
    assert error.value.status_code == status
