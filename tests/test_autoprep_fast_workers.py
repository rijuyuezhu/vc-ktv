"""Fast workers must make progress while four general workers are busy."""

import threading

from tests.test_autoprep_concurrency import eventually
from vc_ktv.app import Store
from vc_ktv.autoprep import AutoPrep


def seed_ready(store, song_id, timing="align"):
    media = f"{song_id}.mp4"
    (store.media / media).write_bytes(b"original")
    with store.lock:
        store.db.execute(
            """INSERT INTO songs(id,bvid,page,title,uploader,cover,duration,
               requester,requested_at,status,prep_state,lyric_choice,
               lyric_timing_mode,provider_lrc,source_lrc_raw,source_file,media_file)
               VALUES (?,'BV0000000001',1,'Sample','UP','',30,'g',?,
                       'ready','downloaded','两行歌词',?,?,'',?,?)""",
            (song_id, song_id, timing, "[00:01]两行歌词", media, media),
        )
        store.db.commit()


def test_fast_lane_claims_only_provider_and_existing_general_lane_unchanged(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.commit()
    seed_ready(store, 1, "align")
    worker = AutoPrep(store, lambda _: 30, max_workers=4, fast_workers=2)
    assert worker.claim("fast") is None
    seed_ready(store, 2, "provider")
    assert worker.claim("fast")["id"] == 2
    assert worker.claim("general")["id"] == 1


def test_four_slow_workers_cannot_block_two_fast_workers(tmp_path, monkeypatch):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.commit()
    for song_id in range(1, 5):
        seed_ready(store, song_id)
    slow_entered = set()
    fast_entered = set()
    gate = threading.Event()
    guard = threading.Lock()

    def slow(source, lyrics, target, archive, duration, *, preset):
        with guard:
            slow_entered.add(int(source.name.split(".")[0]))
        assert gate.wait(7)
        target.write_bytes(b"rendered")
        (archive / "aligned.lrc").write_text("[00:01]两行歌词")
        return {}

    def fast(source, timed, offset, target, archive, duration, **kwargs):
        with guard:
            fast_entered.add(int(source.name.split(".")[0]))
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "aligned.lrc").write_text(timed)
        target.write_bytes(b"fast-rendered")
        return {}

    monkeypatch.setattr("vc_ktv.autoprep.render_provider_video", fast)
    worker = AutoPrep(
        store,
        lambda _: 30,
        enhance=slow,
        max_workers=4,
        fast_workers=2,
    )
    worker.start()
    try:
        assert len(worker.workers) == 6
        assert eventually(lambda: len(slow_entered) == 4)
        assert worker.active_claims == 4
        seed_ready(store, 5, "provider")
        seed_ready(store, 6, "provider")
        assert eventually(lambda: fast_entered == {5, 6})
        assert slow_entered == {1, 2, 3, 4}
        assert eventually(
            lambda: (
                store.db.execute(
                    "SELECT COUNT(*) FROM songs WHERE id>=5 AND prep_state='done'"
                ).fetchone()[0]
                == 2
            )
        )
        assert worker.active_claims == 4
    finally:
        gate.set()
        assert worker.stop()


def test_fast_worker_can_download_known_provider_without_slow_jobs(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.execute(
            """INSERT INTO songs(id,bvid,page,title,uploader,cover,duration,
            requester,requested_at,status,prep_state,lyric_timing_mode)
            VALUES (1,'BV0000000001',1,'Slow','UP','',30,'g',1,'waiting','pending','align')"""
        )
        store.db.execute(
            """INSERT INTO songs(id,bvid,page,title,uploader,cover,duration,
            requester,requested_at,status,prep_state,lyric_timing_mode)
            VALUES (2,'BV0000000001',1,'Fast','UP','',30,'g',2,'waiting','pending','provider')"""
        )
        store.db.commit()
    worker = AutoPrep(store, lambda _: 30, max_workers=4, fast_workers=2)
    assert worker.claim("fast")["id"] == 2
    assert worker.claim("fast") is None
    assert worker.claim("general")["id"] == 1
