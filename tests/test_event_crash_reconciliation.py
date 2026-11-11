"""Process termination between durable event transitions must self-reconcile."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vc_ktv.core.events import Events
from vc_ktv.core.store import Store


def crash(code: str, root: Path, expected: int):
    proc = subprocess.run(
        [sys.executable, "-c", code, str(root)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert proc.returncode == expected, proc.stderr


def test_crash_before_publishing_new_event_cleans_incomplete_room(tmp_path):
    script = """
import os,sys
from pathlib import Path
from vc_ktv.core.events import Events
from vc_ktv.core.store import Store
root=Path(sys.argv[1]);e=Events(root,Store)
old=e._write_atomic
def crash_before_publish(path,text):
    if path == e.pointer: os._exit(53)
    return old(path,text)
e._write_atomic=crash_before_publish
e.create('unfinished','123456')
"""
    crash(script, tmp_path, 53)
    assert (tmp_path / "creating-session").exists()
    restarted = Events(tmp_path, Store)
    assert restarted.active_id is None
    assert restarted.list() == []
    assert list((tmp_path / "sessions").iterdir()) == []
    assert not (tmp_path / "creating-session").exists()
    assert restarted.create("new", "123456")["status"] == "active"
    restarted.current_store.db.close()


def test_crash_after_publishing_pointer_keeps_event(tmp_path):
    script = """
import os,sys
from pathlib import Path
from vc_ktv.core.events import Events
from vc_ktv.core.store import Store
root=Path(sys.argv[1]);e=Events(root,Store)
old=Path.unlink
def crash_at_marker_removal(path,*a,**kw):
    if path == e.creating_marker: os._exit(54)
    return old(path,*a,**kw)
Path.unlink=crash_at_marker_removal
e.create('published','123456')
"""
    crash(script, tmp_path, 54)
    eid = (tmp_path / "active-session").read_text().strip()
    restarted = Events(tmp_path, Store)
    assert restarted.active_id == eid
    assert restarted.metadata()["status"] == "active"
    assert not (tmp_path / "creating-session").exists()
    restarted.current_store.db.close()


@pytest.mark.parametrize("db_committed", [False, True])
def test_interrupted_close_reconciles_before_worker_start(tmp_path, db_committed):
    events = Events(tmp_path, Store)
    events.create("in progress", "123456")
    with events.current_store.lock:
        db = events.current_store.db
        db.execute("INSERT INTO guests(id,name) VALUES ('a','A')")
        db.execute(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,
             requested_at,status,prep_state) VALUES('BVTEST',1,'Test','Uploader','',120,'a',1,'ready','done')"""
        )
        db.commit()
    events.mark_closing()
    if db_committed:
        db.execute("UPDATE songs SET status='closed_unplayed',archive_order=1")
        db.commit()
    events.current_store.db.close()
    restarted = Events(tmp_path, Store)
    assert restarted.active_id is None
    assert not (tmp_path / "active-session").exists()
    assert not (tmp_path / "closing-session").exists()
    assert restarted.list()[0]["status"] == "closed"
    store = Store(tmp_path / "sessions" / restarted.list()[0]["id"])
    try:
        assert tuple(
            store.db.execute("SELECT status,archive_order FROM songs").fetchone()
        ) == ("closed_unplayed", 1)
    finally:
        store.db.close()


def test_interrupted_metadata_close_does_not_reopen_closed_event(tmp_path):
    events = Events(tmp_path, Store)
    entry = events.create("ended", "123456")
    folder = tmp_path / "sessions" / entry["id"]
    events.current_store.db.close()
    info = json.loads((folder / "info.json").read_text())
    info["status"] = "closed"
    (folder / "info.json").write_text(json.dumps(info))
    assert (tmp_path / "active-session").exists()
    restarted = Events(tmp_path, Store)
    assert restarted.active_id is None
    assert not (tmp_path / "active-session").exists()


def test_corrupt_pointer_is_quarantined_without_erasing_data(tmp_path):
    (tmp_path / "active-session").write_text("../invalid\n")
    restarted = Events(tmp_path, Store)
    assert restarted.active_id is None
    assert not (tmp_path / "active-session").exists()
    assert len(list(tmp_path.glob(".invalid-active-session-*"))) == 1
    assert restarted.create("new", "123456")["status"] == "active"
    restarted.current_store.db.close()


def test_legacy_unpublished_active_event_is_reattached(tmp_path):
    event_id = "legacy-orphaned-room"
    folder = tmp_path / "sessions" / event_id
    folder.mkdir(parents=True)
    (folder / "info.json").write_text(
        json.dumps({"id": event_id, "status": "active", "name": "Old"})
    )
    store = Store(folder)
    store.db.close()
    restarted = Events(tmp_path, Store)
    assert restarted.active_id == event_id
    assert (tmp_path / "active-session").read_text().strip() == event_id
    restarted.current_store.db.close()
