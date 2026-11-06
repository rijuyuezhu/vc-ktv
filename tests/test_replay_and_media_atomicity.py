"""Transaction visibility and replay failure rollback boundaries."""

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_manual_casting_queue import add_song, new_app


def test_media_read_waits_for_worker_transaction_to_settle(tmp_path):
    app = new_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "Singer")
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        store = app.state.store
        user_id = store.rows("SELECT id FROM users")[0]["id"]
        song_id = add_song(store, user_id, media="committed.mp4")
        active = admin.post("/api/admin/next").json()["active"]
        assert active["id"] == song_id
        signed_media = active["media_url"]
        assert guest.get(signed_media).content == b"valid-pretend-mp4"

        underlying = app.state.events.active()
        (underlying.media / "uncommitted.mp4").write_bytes(b"UNCOMMITTED")
        started = threading.Event()

        def read_media():
            started.set()
            return guest.get(signed_media)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with underlying.lock:
                underlying.db.execute(
                    "UPDATE songs SET media_file='uncommitted.mp4' WHERE id=?",
                    (song_id,),
                )
                future = pool.submit(read_media)
                assert started.wait(2)
                try:
                    with pytest.raises(FutureTimeout):
                        future.result(timeout=0.4)
                finally:
                    underlying.db.rollback()
            response = future.result(timeout=3)
        assert response.status_code == 200
        assert response.content == b"valid-pretend-mp4"
        assert store.rows("SELECT media_file FROM songs WHERE id=?", (song_id,)) == [
            {"media_file": "committed.mp4"}
        ]


def test_replay_insert_failure_rolls_back_priority_without_media_links(tmp_path):
    app = new_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        store = app.state.store
        store.db.execute("INSERT INTO guests(id,name) VALUES ('singer','Singer')")
        store.db.commit()
        song_id = add_song(store, "singer", status="played", media="baked.mp4")
        (store.media / "source.mp4").write_bytes(b"original-source")
        with store.lock:
            store.db.execute(
                "UPDATE songs SET source_file='source.mp4' WHERE id=?", (song_id,)
            )
            store.db.commit()
        prior = add_song(store, "singer", time=2, media="prior.mp4")
        assert admin.post(f"/api/admin/songs/{prior}/pin-next").status_code == 200
        snapshot = store.rows(
            "SELECT id,priority_next,replay_of,media_file,source_file FROM songs ORDER BY id"
        )
        files_before = {f.name for f in store.media.iterdir()}
        with store.lock:
            store.db.execute(
                """CREATE TRIGGER reject_virtual_replay BEFORE INSERT ON songs
                WHEN NEW.replay_of IS NOT NULL BEGIN
                    SELECT RAISE(ABORT, 'simulated replay insert failure');
                END"""
            )
            store.db.commit()
        response = admin.post(f"/api/admin/songs/{song_id}/replay")
        assert response.status_code == 500
        assert (
            store.rows(
                "SELECT id,priority_next,replay_of,media_file,source_file FROM songs ORDER BY id"
            )
            == snapshot
        )
        assert {f.name for f in store.media.iterdir()} == files_before
        assert (store.media / "baked.mp4").read_bytes() == b"valid-pretend-mp4"
        assert (store.media / "source.mp4").read_bytes() == b"original-source"


def test_lyrics_read_never_observes_uncommitted_worker_value(tmp_path):
    app = new_app(tmp_path)
    with TestClient(app) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        store = app.state.store
        underlying = app.state.events.active()
        with underlying.lock:
            underlying.db.execute(
                "INSERT INTO guests(id,name) VALUES ('singer','Singer')"
            )
            underlying.db.commit()
        song_id = add_song(store, "singer")
        with underlying.lock:
            underlying.db.execute(
                "UPDATE songs SET lyrics='committed' WHERE id=?", (song_id,)
            )
            underlying.db.commit()
        started = threading.Event()

        def read_lyrics():
            started.set()
            return admin.get(f"/api/admin/songs/{song_id}/lyrics")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with underlying.lock:
                underlying.db.execute(
                    "UPDATE songs SET lyrics='uncommitted' WHERE id=?", (song_id,)
                )
                future = pool.submit(read_lyrics)
                assert started.wait(2)
                try:
                    with pytest.raises(FutureTimeout):
                        future.result(timeout=0.4)
                finally:
                    underlying.db.rollback()
            response = future.result(timeout=3)
        assert response.status_code == 200
        assert response.json() == {"text": "committed"}


def test_virtual_replay_of_legacy_video_without_source_uses_video_as_source(tmp_path):
    app = new_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "Singer")
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        store = app.state.store
        owner = store.rows("SELECT id FROM users WHERE username='Singer'")[0]["id"]
        song_id = add_song(store, owner, status="played", media="video-only.mp4")
        with store.lock:
            store.db.execute("UPDATE songs SET source_file=NULL WHERE id=?", (song_id,))
            store.db.commit()
        created = guest.post(f"/api/songs/{song_id}/replay")
        assert created.status_code == 200, created.text
        replay_id = created.json()["id"]
        with store.lock:
            row = store.db.execute(
                "SELECT id,replay_of,media_file,source_file FROM songs WHERE id=?",
                (replay_id,),
            ).fetchone()
            assert row["media_file"] is None
            assert row["source_file"] is None
        assert (
            guest.get(f"/api/songs/{replay_id}/download").content
            == b"valid-pretend-mp4"
        )
        assert (
            guest.get(f"/api/songs/{replay_id}/download/source").content
            == b"valid-pretend-mp4"
        )
