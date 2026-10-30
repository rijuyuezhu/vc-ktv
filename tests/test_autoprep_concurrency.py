"""Bounded song concurrency must not double-claim or lose a busy worker."""

import threading
import time

import pytest

from vc_ktv.app import Store
from vc_ktv.autoprep import AutoPrep


def seed_downloads(store, count):
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.executemany(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,
               requester,requested_at,status,prep_state)
               VALUES('BV0000000001',1,?,?,?,20,'g',?,'waiting','pending')""",
            [(f"Song {n}", "Uploader", "", n) for n in range(count)],
        )
        store.db.commit()


def eventually(condition, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_parallel_claims_and_busy_remain_until_both_finish(tmp_path):
    store = Store(tmp_path / "room")
    seed_downloads(store, 2)
    entered = []
    calls_lock = threading.Lock()
    release_first = threading.Event()
    release_second = threading.Event()

    def download(bvid, page, directory):
        with calls_lock:
            index = len(entered)
            entered.append(directory)
        assert (release_first if index == 0 else release_second).wait(5)
        source = directory / "source.mp4"
        source.write_bytes(f"video-{index}".encode())
        return source

    worker = AutoPrep(store, probe=lambda path: 8, download=download, max_workers=2)
    assert worker.max_workers == 2
    worker.start()
    try:
        assert eventually(lambda: len(entered) == 2)
        with store.lock:
            assert (
                store.db.execute(
                    "SELECT COUNT(*) FROM songs WHERE prep_state='downloading'"
                ).fetchone()[0]
                == 2
            )
            assert worker.active_claims == 2
            assert worker.busy.is_set()
        assert entered[0] != entered[1]
        release_first.set()
        assert eventually(lambda: worker.active_claims == 1)
        assert worker.busy.is_set(), "Do not allow event closure with other worker busy"
        release_second.set()
        assert eventually(lambda: worker.active_claims == 0)
        assert not worker.busy.is_set()
        with store.lock:
            assert (
                store.db.execute(
                    "SELECT COUNT(*) FROM songs WHERE status='ready' AND prep_state='downloaded'"
                ).fetchone()[0]
                == 2
            )
            assert store.db.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 2
    finally:
        release_first.set()
        release_second.set()
        assert worker.stop()
        assert all(not t.is_alive() for t in worker.workers)


def test_default_serial_and_invalid_worker_limits(tmp_path, monkeypatch):
    store = Store(tmp_path / "room")
    monkeypatch.delenv("VC_KTV_PREP_WORKERS", raising=False)
    assert AutoPrep(store, probe=lambda p: 10).max_workers == 1
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", "2")
    assert AutoPrep(store, probe=lambda p: 10).max_workers == 2
    assert AutoPrep(store, probe=lambda p: 10, max_workers=3).max_workers == 3
    assert AutoPrep(store, probe=lambda p: 10, max_workers=4).max_workers == 4
    monkeypatch.setenv("VC_KTV_PREP_WORKERS", "4")
    assert AutoPrep(store, probe=lambda p: 10).max_workers == 4
    for n in (0, 5, -1):
        with pytest.raises(ValueError, match="1, 2, 3 or 4"):
            AutoPrep(store, probe=lambda p: 10, max_workers=n)


def test_two_subtitle_jobs_use_disjoint_artifacts_and_are_both_committed(tmp_path):
    store = Store(tmp_path / "room")
    seed_downloads(store, 2)
    for song_id in (1, 2):
        source = f"{song_id}-source.mp4"
        (store.media / source).write_bytes(f"original-{song_id}".encode())
        with store.lock:
            store.db.execute(
                """UPDATE songs SET status='ready', prep_state='downloaded',
                   source_file=?, media_file=?, lyric_choice='同一句确认歌词', duration=5
                   WHERE id=?""",
                (source, source, song_id),
            )
            store.db.commit()

    entered = []
    entered_lock = threading.Lock()
    releases = [threading.Event(), threading.Event()]

    def enhance(source, lyrics, target, archive, duration, *, preset):
        assert lyrics == "同一句确认歌词"
        with entered_lock:
            index = len(entered)
            entered.append((source, target, archive))
        assert releases[index].wait(5)
        target.write_bytes(source.read_bytes() + b"-subtitled")
        (archive / "aligned.lrc").write_text("[00:01.00]同一句确认歌词")
        return {"rendered_lines": 1}

    worker = AutoPrep(store, probe=lambda path: 5, enhance=enhance, max_workers=2)
    worker.start()
    try:
        assert eventually(lambda: len(entered) == 2)
        assert worker.active_claims == 2 and worker.busy.is_set()
        assert len({item[1] for item in entered}) == 2
        assert len({item[2] for item in entered}) == 2
        releases[0].set()
        assert eventually(lambda: worker.active_claims == 1)
        assert worker.busy.is_set()
        releases[1].set()
        assert eventually(lambda: worker.active_claims == 0)
        with store.lock:
            songs = store.db.execute(
                "SELECT id, prep_state, media_file, lyrics, prep_timing FROM songs ORDER BY id"
            ).fetchall()
        assert len(songs) == 2
        for song in songs:
            assert song["prep_state"] == "done"
            assert song["lyrics"] == "[00:01.00]同一句确认歌词"
            assert (store.media / song["media_file"]).read_bytes() == (
                f"original-{song['id']}-subtitled".encode()
            )
    finally:
        for release in releases:
            release.set()
        assert worker.stop()


def test_three_workers_do_not_unset_busy_until_last_task(tmp_path):
    store = Store(tmp_path / "room")
    seed_downloads(store, 3)
    started = []
    guard = threading.Lock()
    gates = [threading.Event() for _ in range(3)]

    def download(bvid, page, directory):
        with guard:
            index = len(started)
            started.append(directory)
        assert gates[index].wait(5)
        source = directory / "source.mp4"
        source.write_bytes(f"video-{index}".encode())
        return source

    worker = AutoPrep(store, probe=lambda path: 8, download=download, max_workers=3)
    worker.start()
    try:
        assert eventually(lambda: len(started) == 3)
        assert worker.active_claims == 3 and worker.busy.is_set()
        with store.lock:
            assert (
                store.db.execute(
                    "SELECT COUNT(*) FROM songs WHERE prep_state='downloading'"
                ).fetchone()[0]
                == 3
            )
        assert len(set(started)) == 3
        for count in range(2):
            gates[count].set()
            assert eventually(lambda: worker.active_claims == 2 - count)
            assert worker.busy.is_set()
        gates[2].set()
        assert eventually(lambda: worker.active_claims == 0)
        assert not worker.busy.is_set()
        with store.lock:
            assert (
                store.db.execute(
                    "SELECT COUNT(*) FROM songs WHERE status='ready' AND prep_state='downloaded'"
                ).fetchone()[0]
                == 3
            )
    finally:
        for gate in gates:
            gate.set()
        assert worker.stop()


def test_four_workers_start_with_one_song_and_fill_available_slots(tmp_path):
    """Four is a capacity limit, not a minimum batch size or a startup barrier."""
    store = Store(tmp_path / "room")
    seed_downloads(store, 1)
    started = []
    guard = threading.Lock()
    gates = [threading.Event() for _ in range(5)]

    def download(bvid, page, directory):
        with guard:
            index = len(started)
            started.append(directory)
        assert gates[index].wait(10)
        source = directory / "source.mp4"
        source.write_bytes(f"video-{index}".encode())
        return source

    def enqueue(count):
        with store.lock:
            offset = store.db.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
            store.db.executemany(
                """INSERT INTO songs(bvid,page,title,uploader,cover,duration,
                   requester,requested_at,status,prep_state)
                   VALUES('BV0000000001',1,?,?,?,20,'g',?,'waiting','pending')""",
                [(f"Later {i}", "Uploader", "", offset + i) for i in range(count)],
            )
            store.db.commit()

    worker = AutoPrep(store, probe=lambda path: 8, download=download, max_workers=4)
    worker.start()
    try:
        assert len(worker.workers) == 4
        # Immediately process a single song, without waiting for four requests.
        assert eventually(lambda: len(started) == 1)
        assert worker.active_claims == 1 and worker.busy.is_set()

        # The second and third songs are claimed as soon as they arrive.
        enqueue(2)
        assert eventually(lambda: len(started) == 3)
        assert worker.active_claims == 3

        enqueue(2)
        assert eventually(lambda: len(started) == 4)
        assert worker.active_claims == 4
        with store.lock:
            assert (
                store.db.execute(
                    "SELECT COUNT(*) FROM songs WHERE prep_state='pending'"
                ).fetchone()[0]
                == 1
            )
        assert len(set(started)) == 4

        # One task completes; the fifth is claimed without waiting for a batch.
        gates[0].set()
        assert eventually(lambda: len(started) == 5)
        assert worker.active_claims == 4 and worker.busy.is_set()
        assert len(set(started)) == 5

        for gate in gates[1:]:
            gate.set()
        assert eventually(lambda: worker.active_claims == 0)
        assert not worker.busy.is_set()
        with store.lock:
            assert (
                store.db.execute(
                    "SELECT COUNT(*) FROM songs WHERE status='ready' AND prep_state='downloaded'"
                ).fetchone()[0]
                == 5
            )
            assert store.db.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 5
    finally:
        for gate in gates:
            gate.set()
        assert worker.stop()
        assert all(not thread.is_alive() for thread in worker.workers)
