"""Keep the asyncio server responsive under blocked event admission/cleanup."""

import asyncio
import threading
import time

import httpx

from vc_ktv.application.factory import create_app


def test_event_admission_contending_with_worker_does_not_block_loop(tmp_path):
    app = create_app(
        root=tmp_path / "event-loop",
        password="test",
        secret=b"event-loop-review",
        auto_prepare=False,
    )
    app.state.events.create("Synthetic test", "123456")
    events = app.state.events
    store = events.active()
    held = threading.Event()
    release = threading.Event()
    waiting = threading.Event()

    def worker():
        with store.lock:
            held.set()
            assert release.wait(3)

    def contending_request():
        waiting.set()
        with app.state.store.lock:
            pass

    async def check():
        owner = threading.Thread(target=worker)
        contender = threading.Thread(target=contending_request)
        owner.start()
        try:
            assert await asyncio.to_thread(held.wait, 2)
            contender.start()
            assert await asyncio.to_thread(waiting.wait, 2)
            # The contender waits for Store.lock; event admission remains
            # available while it is waiting, and the loop keeps running.
            await asyncio.sleep(0.02)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                start = time.monotonic()
                response = asyncio.create_task(client.get("/api/health"))
                # Release the Store mutex after enough time to distinguish
                # normal coroutine scheduling from a 350 ms event-loop stall.
                release_timer = threading.Timer(0.35, release.set)
                release_timer.start()
                try:
                    await asyncio.sleep(0.03)
                    heartbeat = time.monotonic() - start
                    result = await response
                finally:
                    release.set()
                    release_timer.join(timeout=2)
                assert result.status_code == 200
                assert heartbeat < 0.2, f"event loop stalled for {heartbeat:.3f}s"
        finally:
            release.set()
            owner.join(timeout=2)
            if contender.ident is not None:
                contender.join(timeout=2)
            assert not owner.is_alive()
            assert not contender.is_alive()

    asyncio.run(check())


def test_event_admission_cleanup_does_not_block_loop(tmp_path, monkeypatch):
    app = create_app(
        root=tmp_path / "event-loop-cleanup",
        password="test",
        secret=b"event-loop-cleanup",
        auto_prepare=False,
    )
    app.state.events.create("Synthetic test", "123456")
    events = app.state.events
    entered = threading.Event()
    release = threading.Event()
    old_end = events.end_request

    def slow_end():
        entered.set()
        assert release.wait(3)
        old_end()

    monkeypatch.setattr(events, "end_request", slow_end)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ongoing = asyncio.create_task(client.get("/api/health"))
            assert await asyncio.to_thread(entered.wait, 2)
            release_timer = threading.Timer(0.35, release.set)
            release_timer.start()
            try:
                start = time.monotonic()
                await asyncio.sleep(0.03)
                heartbeat = time.monotonic() - start
                assert heartbeat < 0.2, f"event loop stalled for {heartbeat:.3f}s"
            finally:
                release.set()
                release_timer.join(timeout=2)
            assert (await ongoing).status_code == 200
            assert events.inflight == 0

    asyncio.run(check())


def test_unrelated_health_response_during_an_active_store_transaction(tmp_path):
    """Keep request admission open even while a prior Store transaction runs."""
    app = create_app(
        root=tmp_path / "health-during-store",
        password="test",
        secret=b"health-store-isolation",
        auto_prepare=False,
    )
    app.state.events.create("Synthetic test", "123456")
    entered, release = threading.Event(), threading.Event()

    def holding_request():
        with app.state.store.lock:
            entered.set()
            assert release.wait(2)

    thread = threading.Thread(target=holding_request)
    thread.start()

    async def check():
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                task = asyncio.create_task(client.get("/api/health"))
                try:
                    result = await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
                    assert result.status_code == 200
                    assert not release.is_set(), (
                        "the test Store transaction ended early"
                    )
                finally:
                    release.set()
                    await task
        finally:
            release.set()

    try:
        asyncio.run(check())
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
