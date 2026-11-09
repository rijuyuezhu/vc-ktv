"""Async song and lyric HTTP routes must not block loop on DB/FS admission."""

import asyncio
import threading
import time

import httpx
import pytest

from vc_ktv.application.factory import create_app
from vc_ktv.application.guest_lyrics import LyricSelectionService


def make_app(tmp_path):
    app = create_app(
        root=tmp_path / "requests-loop",
        password="test",
        secret=b"song-requests-loop-test",
        auto_prepare=False,
    )
    app.state.events.create("Test", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "Example song",
            "uploader": "Singer",
            "cover": "",
            "duration": 60,
        }

    app.state.lookup = lookup

    async def resolve(_):
        return 1234

    async def netease_lookup(_):
        return {
            "source": "netease",
            "source_id": "1234",
            "title": "Example song",
            "uploader": "Singer",
            "cover": "",
            "duration": 60,
        }

    app.state.netease_resolve = resolve
    app.state.netease_lookup = netease_lookup
    return app


async def assert_loop_responsive(client, method, path, entered, release, **kwargs):
    pending = asyncio.create_task(client.request(method, path, **kwargs))
    try:
        assert await asyncio.to_thread(entered.wait, 2), "slow operation never started"
        # Confirm the operation is in flight before testing the coroutine loop.
        timer = threading.Timer(0.35, release.set)
        timer.start()
        try:
            start = time.monotonic()
            await asyncio.sleep(0.03)
            heartbeat = time.monotonic() - start
            assert heartbeat < 0.2, f"asyncio loop blocked for {heartbeat:.3f}s"
        finally:
            release.set()
            timer.join(timeout=2)
        return await pending
    finally:
        release.set()
        if not pending.done():
            await pending


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("/api/songs", {"bvid": "BV0000000001", "page": 1}),
        ("/api/netease/songs", {"ref": "1234"}),
    ],
)
def test_song_admission_slow_quota_scan_does_not_block_loop(
    tmp_path, monkeypatch, route, payload
):
    app = make_app(tmp_path)
    quota_checks = []
    entered, release = threading.Event(), threading.Event()

    def slow_check():
        quota_checks.append(time.monotonic())
        if len(quota_checks) == 1:
            entered.set()
            assert release.wait(2)

    monkeypatch.setattr(app.state.events.active().disk_budget, "check", slow_check)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (
                await client.post("/api/join", json={"code": "123456"})
            ).status_code == 200
            register = await client.post(
                "/api/users/register",
                json={
                    "username": "singer",
                    "password": "test-password",
                    "confirm_password": "test-password",
                },
            )
            assert register.status_code == 200, register.text
            return await assert_loop_responsive(
                client, "POST", route, entered, release, json=payload
            )

    response = asyncio.run(check())
    assert response.status_code == 200, response.text
    assert quota_checks
    assert len(app.state.events.active().rows("SELECT id FROM songs")) == 1


def test_lyric_candidate_lookup_waits_for_store_off_loop(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    calls = []
    entered, release = threading.Event(), threading.Event()

    def slow_song_lookup(self, song_id, guest_token):
        calls.append(song_id)
        entered.set()
        assert release.wait(2)
        return {
            "id": song_id,
            "status": "waiting",
            "title": "Example",
            "source_type": "bilibili",
            "requester": "guest",
        }

    monkeypatch.setattr(LyricSelectionService, "own_song", slow_song_lookup)

    async def candidates(_):
        return []

    app.state.candidate_search = candidates

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await assert_loop_responsive(
                client,
                "GET",
                "/api/songs/17/lyrics/candidates?query=Example",
                entered,
                release,
            )

    response = asyncio.run(check())
    assert response.status_code == 200, response.text
    assert calls == [17]
