"""Backing audio is optional, persistent across replays, and independent of normal prep."""

import hashlib
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from vc_ktv.core.store import Store
from vc_ktv.processing.backing_audio import BackingTracks, source_identity


def setup(tmp_path, producer):
    store = Store(tmp_path / "event")
    source = store.media / "1-original.mp4"
    source.write_bytes(b"same original soundtrack" * 100)
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.execute(
            """INSERT INTO songs
               (id,bvid,page,title,uploader,cover,duration,requester,requested_at,
                status,prep_state,source_file,media_file)
               VALUES(1,'BV0000000001',1,'Song','UP','',40,'g',1,'ready',
                      'original',?,?)""",
            (source.name, source.name),
        )
        store.db.execute(
            """INSERT INTO songs
               (id,bvid,page,title,uploader,cover,duration,requester,requested_at,
                status,prep_state,replay_of)
               VALUES(2,'BV0000000001',1,'Replay','UP','',40,'g',2,
                      'played','original',1)"""
        )
        store.db.commit()
    owner = SimpleNamespace(store=store, stop_signal=threading.Event(), active_claims=0)
    model = tmp_path / "model.ckpt"
    model.write_bytes(b"test checkpoint")
    sha = hashlib.sha256(model.read_bytes()).hexdigest()
    service = BackingTracks(
        owner, enabled=True, checkpoint=model, model_sha256=sha, producer=producer
    )
    service.model_source = tmp_path / "model-source"
    service.model_source.mkdir()
    (service.model_source / "utils.py").write_text("# fake model source")
    service.python = Path(sys.executable)
    return service, store, source


def get_row(store, song_id):
    with store.lock:
        return dict(
            store.db.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
        )


def test_result_ready_and_replay_reuses_same_asset(tmp_path):
    called = []

    def generate(source, target, staging, **kwargs):
        called.append(source.name)
        target.write_bytes(b"backing" * 512)

    svc, store, source = setup(tmp_path, generate)
    candidate = svc._candidate()
    assert candidate is not None
    song_id, selected, key = candidate
    assert song_id == 1 and selected == source
    assert svc._candidate() is None
    svc._process(song_id, source, key)
    assert called == [source.name]
    assert svc.state(get_row(store, 1)) == "ready"
    assert svc.state(get_row(store, 2)) == "ready"
    with store.lock:
        record = store.db.execute(
            "SELECT elapsed_seconds FROM backing_tracks WHERE cache_key=?", (key,)
        ).fetchone()
        assert record["elapsed_seconds"] is not None
        assert record["elapsed_seconds"] >= 0
    assert (store.root / "backing" / f"{key}.m4a").read_bytes() == b"backing" * 512
    assert svc._candidate() is None
    assert not list(store.root.glob("vc-ktv-*-backing-*"))


def test_failures_are_retryable_and_do_not_touch_original(tmp_path):
    def fail(*_args, **_kwargs):
        raise RuntimeError("model failed")

    svc, store, source = setup(tmp_path, fail)
    song_id, _, key = svc._candidate()
    svc._process(song_id, source, key)
    assert source.is_file()
    assert svc.state(get_row(store, 1)) == "failed"
    assert svc._candidate() is None
    with store.lock:
        record = store.db.execute(
            "SELECT elapsed_seconds,error FROM backing_tracks WHERE cache_key=?", (key,)
        ).fetchone()
        assert record["elapsed_seconds"] is not None
        assert record["error"] == "RuntimeError"
    assert svc.retry(get_row(store, 1))
    assert svc.state(get_row(store, 1)) == "pending"
    assert (
        store.db.execute(
            "SELECT elapsed_seconds FROM backing_tracks WHERE cache_key=?", (key,)
        ).fetchone()[0]
        is None
    )
    assert svc._candidate() is not None


def test_preempted_job_is_not_published(tmp_path):
    def preempt(_source, target, _staging, **_kwargs):
        svc.cancelled.set()
        target.write_bytes(b"not committed" * 100)

    svc, store, source = setup(tmp_path, preempt)
    song_id, _, key = svc._candidate()
    svc._process(song_id, source, key)
    assert svc.state(get_row(store, 1)) == "pending"
    assert not (store.root / "backing" / f"{key}.m4a").exists()
    assert svc._candidate() is not None


def test_source_change_invalidates_cache(tmp_path):
    def generate(_source, target, _staging, **_kwargs):
        target.write_bytes(b"backing" * 512)

    svc, store, source = setup(tmp_path, generate)
    song_id, _, key = svc._candidate()
    svc._process(song_id, source, key)
    assert svc.state(get_row(store, 1)) == "ready"
    source.write_bytes(b"new original" * 200)
    assert source_identity(source, svc.model_sha256) != key
    assert svc.state(get_row(store, 1)) == "pending"
    assert svc._candidate()[2] != key


def test_processing_recovers_on_worker_restart(tmp_path):
    svc, store, _ = setup(tmp_path, lambda *_args, **_kwargs: None)
    song_id, _, key = svc._candidate()
    with store.lock:
        assert (
            store.db.execute(
                "SELECT status FROM backing_tracks WHERE cache_key=?", (key,)
            ).fetchone()[0]
            == "processing"
        )
    svc.start()
    try:
        with store.lock:
            status = store.db.execute(
                "SELECT status FROM backing_tracks WHERE cache_key=?", (key,)
            ).fetchone()[0]
            assert status in ("pending", "processing")
    finally:
        svc.owner.stop_signal.set()
        assert svc.stop()


def test_backing_cache_table_survives_store_reopen(tmp_path):
    svc, store, source = setup(
        tmp_path, lambda _, target, __, **kw: target.write_bytes(b"x" * 2048)
    )
    song_id, _, key = svc._candidate()
    svc._process(song_id, source, key)
    store.db.close()
    reopened = Store(store.root)
    assert (
        reopened.db.execute(
            "SELECT status FROM backing_tracks WHERE cache_key=?", (key,)
        ).fetchone()[0]
        == "ready"
    )
    reopened.db.close()


def test_audio_release_retains_cover_but_deletes_backing(tmp_path):
    from vc_ktv.core.events import Events

    events = Events(tmp_path / "catalog", Store)
    entry = events.create("Test event")
    folder = events.base / entry["id"]
    backing = folder / "backing"
    backing.mkdir()
    (backing / "asset.m4a").write_bytes(b"a" * 1200)
    covers = folder / "media" / "covers"
    covers.mkdir()
    (covers / "1.jpg").write_bytes(b"cover")
    events.close()

    report = events.release_media(entry["id"], "song\n")
    assert report["freed_bytes"] == 1200
    assert not backing.exists()
    assert (covers / "1.jpg").read_bytes() == b"cover"
    assert events.release_media(entry["id"], "song\n")["freed_bytes"] == 0


def test_default_disabled_and_invalid_optional_checkpoint(tmp_path, monkeypatch):
    from vc_ktv.processing.worker import AutoPrep

    monkeypatch.delenv("VC_KTV_BACKING_TRACKS", raising=False)
    store = Store(tmp_path / "room")
    owner = AutoPrep(store, lambda _: 1)
    assert not owner.backing.enabled
    owner.backing.start()
    assert owner.backing.thread is None
    owner.backing.enabled = True
    owner.backing.checkpoint = tmp_path / "nonexistent.ckpt"
    owner.backing.start()
    assert owner.backing.thread is None
    assert owner.backing.unavailable


def test_cancel_preempts_running_attempt_and_reschedules(tmp_path):
    import time

    entered = threading.Event()
    exited = threading.Event()
    attempts = []

    def generate(_source, target, _staging, **_kwargs):
        attempts.append(1)
        entered.set()
        while not svc.cancelled.wait(0.01):
            if len(attempts) > 1:
                break
        target.write_bytes(b"sample" * 500)
        exited.set()

    svc, store, source = setup(tmp_path, generate)
    svc.start()
    try:
        assert entered.wait(4)
        with store.lock:
            svc.owner.active_claims += 1
        svc.cancel_current()
        assert exited.wait(4)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if svc.state(get_row(store, 1)) == "pending":
                break
            time.sleep(0.01)
        assert svc.state(get_row(store, 1)) == "pending"
        assert not list((store.root / "backing").glob("*.m4a"))
    finally:
        svc.owner.stop_signal.set()
        assert svc.stop()


def test_autoprep_starts_optional_backing_worker_without_blocking_songs(tmp_path):
    import time

    from vc_ktv.processing.worker import AutoPrep

    def generate(_source, target, _staging, **_kwargs):
        target.write_bytes(b"backing" * 512)

    svc, store, _ = setup(tmp_path, generate)
    owner = AutoPrep(store, lambda path: 1, max_workers=1)
    svc.owner = owner
    owner.backing = svc
    owner.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if svc.state(get_row(store, 1)) == "ready":
                break
            time.sleep(0.02)
        assert svc.state(get_row(store, 1)) == "ready"
        assert not owner.busy.is_set()
        assert (
            store.db.execute("SELECT status FROM songs WHERE id=1").fetchone()[0]
            == "ready"
        )
    finally:
        assert owner.stop()


def test_release_does_not_follow_backing_directory_symlink(tmp_path):
    import pytest
    from fastapi import HTTPException

    from vc_ktv.core.events import Events

    events = Events(tmp_path / "sessions", Store)
    info = events.create("Event")
    folder = events.base / info["id"]
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.m4a").write_bytes(b"outside")
    (folder / "backing").symlink_to(external, target_is_directory=True)
    (folder / "media" / "video.mp4").write_bytes(b"video")
    events.close()
    with pytest.raises(HTTPException) as error:
        events.release_media(info["id"], "song\n")
    assert error.value.status_code == 409
    assert (folder / "media" / "video.mp4").is_file()
    assert (external / "secret.m4a").read_bytes() == b"outside"


def test_invalid_cached_aac_is_requeued_and_regenerated(tmp_path):
    def generate(_source, target, _staging, **kwargs):
        target.write_bytes(b"valid AAC content" * 200)

    svc, store, source = setup(tmp_path, generate)
    song_id, _, key = svc._candidate()
    svc._process(song_id, source, key)
    file = store.root / "backing" / f"{key}.m4a"
    assert svc.state(get_row(store, 1)) == "ready"
    file.write_bytes(b"damaged")
    assert svc.state(get_row(store, 1)) == "pending"
    candidate = svc._candidate()
    assert candidate and candidate[2] == key
    svc._process(*candidate)
    assert svc.state(get_row(store, 1)) == "ready"
    assert file.stat().st_size > 1024


def test_symlinked_cached_aac_is_regenerated_without_following_link(tmp_path):
    def generate(_source, target, _staging, **kwargs):
        target.write_bytes(b"safe output" * 210)

    svc, store, source = setup(tmp_path, generate)
    song_id, _, key = svc._candidate()
    svc._process(song_id, source, key)
    file = store.root / "backing" / f"{key}.m4a"
    file.unlink()
    outside = tmp_path / "external.m4a"
    outside.write_bytes(b"external untouched" * 200)
    file.symlink_to(outside)
    assert svc.state(get_row(store, 1)) == "pending"
    candidate = svc._candidate()
    assert candidate is not None
    svc._process(*candidate)
    assert outside.read_bytes() == b"external untouched" * 200
    assert not file.is_symlink()
    assert svc.state(get_row(store, 1)) == "ready"


def test_backing_runs_consecutively_under_continuous_prep_load(tmp_path):
    import time

    def generate(_source, target, _staging, **_kwargs):
        target.write_bytes(b"backing" * 512)

    svc, store, _ = setup(tmp_path, generate)
    other = store.media / "3-original.mp4"
    other.write_bytes(b"another original" * 200)
    with store.lock:
        store.db.execute(
            """INSERT INTO songs
               (id,bvid,page,title,uploader,cover,duration,requester,requested_at,
                status,prep_state,source_file,media_file)
               VALUES(3,'BV0000000003',1,'More','UP','',50,'g',3,
                      'ready','original',?,?)""",
            (other.name, other.name),
        )
        store.db.execute(
            """INSERT INTO songs
               (id,bvid,page,title,uploader,cover,duration,requester,requested_at,status)
               VALUES(4,'BV0000000004',1,'Downloading','UP','',50,'g',4,'waiting')"""
        )
        store.db.commit()
    svc.owner.active_claims = 2
    svc.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(svc.state(get_row(store, i)) == "ready" for i in (1, 3)):
                break
            time.sleep(0.02)
        assert all(svc.state(get_row(store, i)) == "ready" for i in (1, 3))
        assert svc.owner.active_claims == 2
        assert svc._candidate() is None
    finally:
        svc.owner.stop_signal.set()
        assert svc.stop()


def test_backing_follows_tv_priority_and_personal_song_order(tmp_path):
    svc, store, _ = setup(tmp_path, lambda *_a, **_kw: None)
    for song_id, ordered_at in ((3, 3), (4, 4)):
        source = store.media / f"{song_id}-original.mp4"
        source.write_bytes(b"audio" * 400)
        with store.lock:
            store.db.execute(
                """INSERT INTO songs
                   (id,bvid,page,title,uploader,cover,duration,requester,requested_at,
                    status,prep_state,source_file,media_file,personal_order)
                   VALUES(?,'BV0000000003',1,'Other','UP','',50,'g',?,
                          'ready','done',?,?,?)""",
                (song_id, ordered_at, source.name, source.name, ordered_at),
            )
            store.db.commit()
    store.save_settings(None, None, require_backing_ready=True)
    assert svc._candidate()[0] == 1  # Include songs still awaiting subtitles.
    with store.lock:
        store.db.execute("UPDATE backing_tracks SET status='failed'")
        store.db.execute("UPDATE songs SET personal_order=0 WHERE id=4")
        store.db.commit()
    assert svc._candidate()[0] == 4


def test_played_songs_are_not_reclaimed_but_new_replays_share_cache(tmp_path):
    svc, store, source = setup(
        tmp_path, lambda _, t, __, **kw: t.write_bytes(b"x" * 2048)
    )
    assert svc._candidate() is not None
    with store.lock:
        store.db.execute("UPDATE songs SET status='played' WHERE id=1")
        # Simulate an interrupted attempt that recovered into pending.
        store.db.execute("UPDATE backing_tracks SET status='pending'")
        store.db.commit()
    assert svc._candidate() is None
    with store.lock:
        store.db.execute("UPDATE songs SET status='ready' WHERE id=2")
        store.db.commit()
    candidate = svc._candidate()
    assert candidate and candidate[0] == 2 and candidate[1] == source
    svc._process(*candidate)
    assert svc.state(get_row(store, 1)) == "ready"
    assert svc.state(get_row(store, 2)) == "ready"


def test_auto_prep_claim_does_not_cancel_inflight_backing(tmp_path, monkeypatch):
    import vc_ktv.processing.worker as worker_module
    from vc_ktv.processing.worker import AutoPrep

    store = Store(tmp_path / "room")
    owner = AutoPrep(store, lambda _: 1, max_workers=1)
    cancelled = []
    monkeypatch.setattr(
        owner.backing, "cancel_current", lambda *_: cancelled.append(True)
    )
    monkeypatch.setattr(
        worker_module, "claim_next_locked", lambda *_: {"id": 42, "status": "waiting"}
    )
    assert owner.claim() is not None
    assert owner.active_claims == 1
    assert cancelled == []


def test_backing_prioritizes_late_joiner_before_ready_old_songs(tmp_path):
    svc, store, _ = setup(tmp_path, lambda *_a, **_kw: None)
    store.db.execute("UPDATE guests SET plays=6 WHERE id='g'")
    store.db.execute("UPDATE songs SET prep_state='done' WHERE id=1")
    store.db.execute("INSERT INTO guests(id,name) VALUES('new','New')")
    source = store.media / "3-original.mp4"
    source.write_bytes(b"new audio" * 400)
    store.db.execute(
        """INSERT INTO songs
           (id,bvid,page,title,uploader,cover,duration,requester,requested_at,
            status,prep_state,source_file,media_file)
           VALUES(3,'BV0000000003',1,'New','UP','',50,'new',3,
                  'ready','downloaded',?,?)""",
        (source.name, source.name),
    )
    store.db.commit()
    assert [song["id"] for song in store.fair_queue()] == [1]
    assert svc._candidate()[0] == 3
