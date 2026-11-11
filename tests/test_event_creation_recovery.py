"""Failed event creation must not leak a phantom active session."""

from pathlib import Path

import pytest

from tests.test_events import make_app


def test_partial_active_pointer_write_does_not_poison_restart(tmp_path, monkeypatch):
    root = tmp_path / "room"
    app = make_app(root)
    events = app.state.events
    original_write = Path.write_text

    def fail_pointer_write(path, contents, *args, **kwargs):
        if path.parent == root and path.name.startswith(".active-session-"):
            original_write(path, "", *args, **kwargs)
            raise OSError("simulated partial pointer write")
        return original_write(path, contents, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail_pointer_write)
        with pytest.raises(OSError, match="partial pointer"):
            events.create("unfinished", "123456")

    assert events.active_id is None
    assert events.current_store is None
    assert not events.pointer.exists()
    assert events.list() == []
    assert list(events.base.iterdir()) == []
    restarted = make_app(root)
    assert restarted.state.events.active_id is None
    assert restarted.state.events.create("recovered", "123456")["status"] == "active"


def test_failed_store_construction_does_not_leave_phantom_event(tmp_path):
    root = tmp_path / "room"
    events = make_app(root).state.events
    real_factory = events.store_factory

    def fail_store(folder):
        built = real_factory(folder)
        built.db.close()
        raise OSError("simulated store initialization failure")

    events.store_factory = fail_store
    with pytest.raises(OSError, match="initialization failure"):
        events.create("unfinished", "123456")

    assert events.active_id is None
    assert events.current_store is None
    assert not events.pointer.exists()
    assert events.list() == []
    assert list(events.base.iterdir()) == []

    restarted = make_app(root)
    assert restarted.state.events.active_id is None


def test_failed_event_metadata_write_removes_partial_session(tmp_path, monkeypatch):
    root = tmp_path / "room"
    events = make_app(root).state.events
    original_write = Path.write_text

    def fail_info_write(path, contents, *args, **kwargs):
        if path.name == "info.json" and path.parent.parent == events.base:
            original_write(path, "{", *args, **kwargs)
            raise OSError("simulated partial metadata write")
        return original_write(path, contents, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail_info_write)
        with pytest.raises(OSError, match="partial metadata"):
            events.create("unfinished", "123456")

    assert events.active_id is None
    assert events.current_store is None
    assert not events.pointer.exists()
    assert events.list() == []
    assert list(events.base.iterdir()) == []
    assert make_app(root).state.events.active_id is None
