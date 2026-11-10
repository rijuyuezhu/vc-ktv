"""Upload, quota and TextGrid work may take time, but must not stop asyncio."""

import asyncio
import time

import httpx

from vc_ktv.application import admin_preparation
from vc_ktv.application.factory import create_app


def make_app(tmp_path):
    app = create_app(
        root=tmp_path / "media-event-loop",
        password="test",
        secret=b"media-loop-test-key",
        auto_prepare=False,
    )
    app.state.events.create("Test", "123456")
    return app


async def exercise_admin_request(app, path, files):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        login = await client.post("/api/admin/login", json={"password": "test"})
        assert login.status_code == 200
        start = time.monotonic()
        request = asyncio.create_task(client.post(path, files=files))
        await asyncio.sleep(0.03)
        heartbeat = time.monotonic() - start
        response = await request
        assert heartbeat < 0.2, f"asyncio loop stalled for {heartbeat:.3f}s"
        return response


def test_upload_quota_scan_and_file_publish_do_not_block_loop(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    store = app.state.events.active()
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES ('guest','Guest')")
        store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at) "
            "VALUES ('BV0000000001',1,'Test','Guest','',60,'guest',1)"
        )
        song_id = store.db.execute("SELECT id FROM songs").fetchone()[0]
        store.db.commit()

    calls = []

    def slow_budget_check():
        calls.append(time.monotonic())
        if len(calls) == 1:
            time.sleep(0.35)

    monkeypatch.setattr(store.disk_budget, "check", slow_budget_check)
    monkeypatch.setattr(
        admin_preparation, "normalize_original_codec", lambda path, **kw: path
    )
    app.state.probe = lambda path: 60

    response = asyncio.run(
        exercise_admin_request(
            app,
            f"/api/admin/songs/{song_id}/media",
            {"file": ("large.mp4", b"v" * (2 * 1024 * 1024), "video/mp4")},
        )
    )
    assert response.status_code == 200, response.text
    assert response.json()["bytes"] == 2 * 1024 * 1024
    assert calls
    assert (
        store.rows("SELECT status FROM songs WHERE id=?", (song_id,))[0]["status"]
        == "ready"
    )


def test_textgrid_synchronous_import_does_not_block_loop(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    calls = []

    def slow_import(self, song_id, content, offset):
        calls.append((song_id, content, offset))
        time.sleep(0.35)
        return {"id": song_id, "ok": True}

    monkeypatch.setattr(
        admin_preparation.AdminPreparationService, "import_textgrid", slow_import
    )
    response = asyncio.run(
        exercise_admin_request(
            app,
            "/api/admin/songs/7/alignment/import?offset=-0.3",
            {"file": ("alignment.TextGrid", b"synthetic", "text/plain")},
        )
    )
    assert response.status_code == 200, response.text
    assert calls == [(7, b"synthetic", -0.3)]
