"""Media responses must survive a replacement while the HTTP body is pending."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.responses import FileResponse

from tests.helpers import join_and_register
from tests.test_manual_casting_queue import add_song, new_app
from vc_ktv.application.common import PinnedFileResponse


def test_download_survives_media_replacement_before_file_open(tmp_path, monkeypatch):
    app = new_app(tmp_path)
    app.state.probe = lambda _: 60
    with (
        TestClient(app) as admin,
        TestClient(app, raise_server_exceptions=False) as guest,
    ):
        join_and_register(guest, "Singer")
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        store = app.state.store
        user_id = store.rows("SELECT id FROM users")[0]["id"]
        song_id = add_song(store, user_id, media="previous.mp4")
        previous = store.media / "previous.mp4"
        entered = threading.Event()
        release = threading.Event()
        original = FileResponse.__call__
        pinned = []

        async def paused_file(self, scope, receive, send):
            if Path(self.path) == previous or Path(self.path).name.startswith(
                ".vc-ktv-inflight-"
            ):
                pinned.append(Path(self.path))
                entered.set()
                await anyio.to_thread.run_sync(release.wait, 5)
            return await original(self, scope, receive, send)

        monkeypatch.setattr(FileResponse, "__call__", paused_file)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(guest.get, f"/api/songs/{song_id}/download")
            try:
                assert entered.wait(3)
                replaced = admin.post(
                    f"/api/admin/songs/{song_id}/media",
                    files={"file": ("new.mp4", b"new-media", "video/mp4")},
                )
                assert replaced.status_code == 200, replaced.text
                assert not previous.exists()
            finally:
                release.set()
            response = pending.result(timeout=7)
        assert response.status_code == 200
        assert response.content == b"valid-pretend-mp4"
        assert pinned and not pinned[0].exists()  # Stream link removed after send.
        part = guest.get(
            f"/api/songs/{song_id}/download", headers={"Range": "bytes=0-2"}
        )
        assert part.status_code == 206
        assert part.content == b"new"
        assert not list(store.media.glob(".vc-ktv-inflight-*"))


def test_pinned_response_disables_deferred_pathsend(tmp_path):
    media = tmp_path / ".vc-ktv-inflight-demo.mp4"
    media.write_bytes(b"video")
    response = PinnedFileResponse(media, media_type="video/mp4")
    emitted = []

    async def send(message):
        emitted.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    anyio.run(
        response,
        {
            "type": "http",
            "method": "GET",
            "headers": [],
            "asgi": {"spec_version": "2.4"},
            "extensions": {"http.response.pathsend": {}},
        },
        receive,
        send,
    )
    assert (
        b"".join(m["body"] for m in emitted if m["type"] == "http.response.body")
        == b"video"
    )
    assert not any(m["type"] == "http.response.pathsend" for m in emitted)
    assert not media.exists()


def test_pinned_response_removes_link_on_stream_error(tmp_path):
    pinned = tmp_path / ".vc-ktv-inflight-failure.mp4"
    pinned.write_bytes(b"video")
    response = PinnedFileResponse(pinned, media_type="video/mp4")

    async def fail_send(message):
        raise RuntimeError("connection failed")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def run():
        await response(
            {
                "type": "http",
                "method": "GET",
                "headers": [],
                "asgi": {"spec_version": "2.4"},
            },
            receive,
            fail_send,
        )

    with pytest.raises(RuntimeError, match="connection failed"):
        anyio.run(run)
    assert not pinned.exists()
