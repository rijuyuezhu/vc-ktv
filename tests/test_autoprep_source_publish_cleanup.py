"""Failed source publishing must not leak media files or SQLite transactions."""

import shutil
import sqlite3

import pytest

from vc_ktv.app import Store
from vc_ktv.autoprep import AutoPrep


def pending_source(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('a','A')")
        song_id = store.db.execute(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at)
               VALUES('BV0000000001',1,'Song','UP','',2,'a',1)"""
        ).lastrowid
        store.db.commit()

    def download(bvid, page, folder):
        source = folder / "source.mp4"
        source.write_bytes(b"source video")
        return source

    worker = AutoPrep(store, lambda path: 2, download=download)
    claimed = worker.claim()
    assert claimed["id"] == song_id
    return store, worker, claimed, song_id


def assert_unpublished(store, song_id):
    assert list(store.media.iterdir()) == []
    assert store.rows(
        "SELECT media_file,source_file,status FROM songs WHERE id=?", (song_id,)
    ) == [{"media_file": None, "source_file": None, "status": "waiting"}]


def test_copy_failure_does_not_leave_partial_original_video(tmp_path, monkeypatch):
    store, worker, claimed, song_id = pending_source(tmp_path)

    def fail_copy(source, destination, *args, **kwargs):
        destination.write_bytes(b"incomplete media bytes")
        raise OSError("simulated disk full during copy")

    monkeypatch.setattr(shutil, "copyfile", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        worker.prepare_source(claimed)
    assert_unpublished(store, song_id)


def test_database_publication_failure_does_not_leave_source_video(tmp_path):
    store, worker, claimed, song_id = pending_source(tmp_path)
    original_db = store.db

    class FailPublish:
        def execute(self, sql, args=()):
            if sql.startswith("UPDATE songs SET media_file="):
                raise sqlite3.OperationalError("simulated DB publication failure")
            return original_db.execute(sql, args)

        def __getattr__(self, name):
            return getattr(original_db, name)

    store.db = FailPublish()
    with pytest.raises(sqlite3.OperationalError, match="publication failure"):
        worker.prepare_source(claimed)
    assert_unpublished(store, song_id)


def test_failed_commit_rolls_back_media_pointer_and_removes_video(tmp_path):
    store, worker, claimed, song_id = pending_source(tmp_path)
    original_db = store.db

    class FailCommit:
        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

        def __getattr__(self, name):
            return getattr(original_db, name)

    store.db = FailCommit()
    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        worker.prepare_source(claimed)

    assert not original_db.in_transaction
    assert_unpublished(store, song_id)
