"""SSE notifier uses bounded wakeups; command state stays in SQLite."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from tests.helpers import primary_tv_token
from tests.test_ui_tv_remote import login, room
from vc_ktv.application.tv_notifications import TVNotifications


def test_sse_wakeups_are_thread_safe_bounded_and_unsubscribed():
    notifier = TVNotifications()

    async def run():
        with notifier.listen("tv") as first, notifier.listen("tv") as second:
            with ThreadPoolExecutor(max_workers=1) as pool:
                await asyncio.get_running_loop().run_in_executor(
                    pool, lambda: [notifier.notify("tv") for _ in range(100)]
                )
            assert first.qsize() == 1
            assert second.qsize() == 1
            assert await asyncio.wait_for(first.get(), 1) is True
            assert await asyncio.wait_for(second.get(), 1) is True
            notifier.notify("admin")
            await asyncio.sleep(0)
            assert first.empty() and second.empty()
        notifier.notify("tv")
        await asyncio.sleep(0)
        assert first.empty() and second.empty()
        assert not notifier._listeners["tv"]

    asyncio.run(run())


def test_sse_endpoints_require_current_primary_or_admin(tmp_path):
    app = room(tmp_path)
    with TestClient(app) as admin, TestClient(app) as outsider:
        assert (
            outsider.get("/api/tv/events", params={"token": "bad"}).status_code == 401
        )
        assert outsider.get("/api/admin/tv/events").status_code == 401
        login(admin)
        # Authenticate before starting the stream; actual long-lived connections
        # are tested through a real disposable server and Chromium.
        token = primary_tv_token(admin)
        assert app.state.tv_session.auth.read_signed(token, "tv-control")
        assert app.state.tv_session.events.active_id


def test_command_and_ack_wake_the_respective_sse_channels(tmp_path):
    app = room(tmp_path)
    with TestClient(app) as admin, TestClient(app) as tv:
        login(admin)
        token = primary_tv_token(admin)
        first = tv.post(
            "/api/tv/report",
            params={"token": token},
            json={
                "ack_seq": 0,
                "active_id": None,
                "paused": True,
                "position": 0,
                "duration": None,
                "volume": 100,
                "offset": 0,
                "opacity": 15,
                "smooth": False,
                "started": True,
            },
        )
        assert first.status_code == 200, first.text
        notifier = app.state.tv_session.notifications

        async def run():
            with notifier.listen("tv") as wake_tv:
                command = await asyncio.to_thread(
                    admin.post,
                    "/api/admin/tv/remote",
                    json={"action": "opacity", "value": 25},
                )
                assert command.status_code == 200, command.text
                assert await asyncio.wait_for(wake_tv.get(), 1)
            with notifier.listen("admin") as wake_admin:
                ack = await asyncio.to_thread(
                    tv.post,
                    "/api/tv/report",
                    params={"token": token},
                    json={
                        "ack_seq": command.json()["seq"],
                        "active_id": None,
                        "paused": True,
                        "position": 0,
                        "duration": None,
                        "volume": 100,
                        "offset": 0,
                        "opacity": 25,
                        "smooth": False,
                        "started": True,
                    },
                )
                assert ack.status_code == 200, ack.text
                assert await asyncio.wait_for(wake_admin.get(), 1)
            assert not admin.get("/api/admin/tv/remote").json()["pending"]

        asyncio.run(run())
