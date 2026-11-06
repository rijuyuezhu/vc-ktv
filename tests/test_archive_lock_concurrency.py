"""Closed-event disk work must not block active event requests or other events."""

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.test_events import admin_login, make_app


@pytest.mark.parametrize("operation", ["release", "delete"])
def test_slow_archive_cleanup_does_not_hold_global_event_lock(
    tmp_path, monkeypatch, operation
):
    app = make_app(tmp_path / "room")
    with TestClient(app) as admin:
        admin_login(admin)
        first = admin.post("/api/admin/events", json={"name": "First"}).json()
        assert admin.post("/api/admin/events/close").status_code == 200
        second = admin.post("/api/admin/events", json={"name": "Second"}).json()
        directory = tmp_path / "room" / "sessions" / first["id"]
        media = directory / "media" / "stuck.mp4"
        media.write_bytes(b"media")
        entered = threading.Event()
        release = threading.Event()
        old_rmtree = shutil.rmtree
        old_unlink = Path.unlink

        def delayed_rmtree(path, *args, **kwargs):
            if Path(path) == directory:
                entered.set()
                assert release.wait(5)
            return old_rmtree(path, *args, **kwargs)

        def delayed_unlink(path, *args, **kwargs):
            if path == media:
                entered.set()
                assert release.wait(5)
            return old_unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            if operation == "release":
                patch.setattr(Path, "unlink", delayed_unlink)
            else:
                patch.setattr(shutil, "rmtree", delayed_rmtree)
            errors = []

            def cleanup():
                try:
                    if operation == "release":
                        app.state.events.release_media(
                            first["id"], "\ufefforder,title\n"
                        )
                    else:
                        app.state.events.delete_closed(first["id"])
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=cleanup)
            thread.start()
            try:
                assert entered.wait(3)
                with pytest.raises(HTTPException) as duplicate:
                    app.state.events.delete_closed(first["id"])
                assert duplicate.value.status_code == 409
                with ThreadPoolExecutor(max_workers=2) as executor:
                    api = executor.submit(admin.get, "/api/admin/state")
                    listing = executor.submit(admin.get, "/api/admin/events")
                    assert api.result(timeout=1).status_code == 200
                    entries = listing.result(timeout=1).json()["events"]
                    assert second["id"] in {entry["id"] for entry in entries}
            finally:
                release.set()
                thread.join(timeout=6)
            assert not thread.is_alive()
            assert not errors, errors
        assert not media.exists()
        if operation == "release":
            assert directory.exists()
        else:
            assert not directory.exists()


def test_failed_cleanup_releases_same_event_guard(tmp_path, monkeypatch):
    app = make_app(tmp_path / "room")
    events = app.state.events
    first = events.create("First")
    events.close()
    folder = tmp_path / "room" / "sessions" / first["id"]

    def fail(*args, **kwargs):
        raise OSError("simulated disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", fail)
        with pytest.raises(OSError):
            events.delete_closed(first["id"])
    assert folder.exists()
    assert events.delete_closed(first["id"]) == {"ok": True}
    assert not folder.exists()
