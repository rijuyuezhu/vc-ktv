"""Closing an event must leave a usable active event if persistence fails."""

import json
import sqlite3
from pathlib import Path

import pytest

from tests.test_events import make_app


def active_event(tmp_path):
    root = tmp_path / "room"
    app = make_app(root)
    events = app.state.events
    entry = events.create("unfinished close", "123456")
    return root, events, entry


def assert_still_active(root, events, entry):
    assert events.active_id == entry["id"]
    assert events.current_store is not None
    assert events.current_store.db.execute("SELECT 1").fetchone()[0] == 1
    assert events.pointer.read_text().strip() == entry["id"]
    assert events.metadata()["status"] == "active"
    assert events.list()[0]["status"] == "active"
    reopened = make_app(root)
    assert reopened.state.events.active_id == entry["id"]
    reopened.state.events.current_store.db.close()


def test_pointer_unlink_failure_does_not_close_event(tmp_path, monkeypatch):
    root, events, entry = active_event(tmp_path)
    original_unlink = Path.unlink
    injected = []

    def fail_unlink(path, *args, **kwargs):
        if path == events.pointer:
            injected.append(path)
            raise PermissionError("simulated pointer unlink failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(PermissionError, match="pointer unlink"):
            events.close()

    assert injected
    assert_still_active(root, events, entry)
    events.close()
    assert events.active_id is None
    assert events.list()[0]["status"] == "closed"
    assert make_app(root).state.events.active_id is None


def test_partial_metadata_write_does_not_corrupt_active_event(tmp_path, monkeypatch):
    root, events, entry = active_event(tmp_path)
    original_write = Path.write_text
    event_folder = events.base / entry["id"]
    injected = []

    def fail_write(path, contents, *args, **kwargs):
        if path.parent == event_folder and '"status": "closed"' in contents:
            injected.append(path)
            original_write(path, "{", *args, **kwargs)
            raise OSError("simulated partial closed metadata write")
        return original_write(path, contents, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail_write)
        with pytest.raises(OSError, match="partial closed metadata"):
            events.close()

    assert injected
    assert json.loads((event_folder / "info.json").read_text())["status"] == "active"
    assert_still_active(root, events, entry)
    events.close()


def test_failed_db_commit_does_not_publish_closed_metadata(tmp_path):
    root, events, entry = active_event(tmp_path)
    store = events.current_store
    real_db = store.db

    class FailCommit:
        def commit(self):
            raise sqlite3.OperationalError("simulated close db commit failure")

        def __getattr__(self, name):
            return getattr(real_db, name)

    store.db = FailCommit()
    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        events.close()
    store.db = real_db
    assert_still_active(root, events, entry)
    events.close()


def test_partial_legacy_close_marker_does_not_close_legacy(tmp_path, monkeypatch):
    from vc_ktv.core.store import Store

    root = tmp_path / "legacy-room"
    legacy = Store(root)
    legacy.db.close()
    events = make_app(root).state.events
    assert events.active_id == "legacy"
    original_write = Path.write_text
    injected = []

    def fail_write(path, contents, *args, **kwargs):
        if path.parent == root and path.name.startswith(".legacy.closed-"):
            injected.append(path)
            original_write(path, "c", *args, **kwargs)
            raise OSError("simulated partial legacy marker write")
        return original_write(path, contents, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail_write)
        with pytest.raises(OSError, match="legacy marker"):
            events.close()

    assert injected
    assert events.active_id == "legacy"
    assert events.current_store.db.execute("SELECT 1").fetchone()[0] == 1
    assert not (root / "legacy.closed").exists()
    assert make_app(root).state.events.active_id == "legacy"
    assert not list(root.glob(".legacy.closed-*.tmp"))
    events.close()
    assert make_app(root).state.events.active_id is None
