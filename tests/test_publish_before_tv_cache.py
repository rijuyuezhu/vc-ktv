"""Publishing a ready song does not wait for optional TV-cache work."""

import json
import threading
import time

import pytest

import vc_ktv.processing.worker as prep
from vc_ktv.application.media import MediaService
from vc_ktv.core.store import Store


def prepared(tmp_path):
    store = Store(tmp_path / "room")
    source = store.media / "original.mp4"
    source.write_bytes(b"ORIGINAL" * 2000)
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('a','A')")
        song_id = store.db.execute(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,
                requested_at,status,source_file,media_file,prep_state,lyric_choice)
               VALUES('BV0000000001',1,'歌','UP','',180,'a',1,'ready',?,?,'downloaded','歌词')""",
            (source.name, source.name),
        ).lastrowid
        store.db.commit()

    def render(source, lyrics, destination, archive, duration, *, preset):
        destination.write_bytes(b"BAKED")
        (archive / "aligned.lrc").write_text("lyrics")
        subtitles = archive / "pipeline" / "subtitles"
        subtitles.mkdir(parents=True)
        for name in ("timeline.json", "display_plan.json", "style.json"):
            (subtitles / name).write_text("{}")
        return {"rendered": True}

    worker = prep.AutoPrep(store, lambda path: 180, enhance=render)
    return worker, store, song_id


def metadata(store, song_id):
    attempt = next(
        (store.media / f"{song_id}-auto-alignment").glob("attempt-*/attempt.json")
    )
    return json.loads(attempt.read_text())


def test_song_is_ready_while_tv_cache_still_builds(tmp_path, monkeypatch):
    worker, store, song_id = prepared(tmp_path)
    started = threading.Event()
    resume = threading.Event()

    def cache(*args, **kwargs):
        started.set()
        if not resume.wait(timeout=5):
            raise RuntimeError("cache test release timeout")
        (args[1] / "tv-source.mp4").write_bytes(b"CACHE")

    monkeypatch.setattr(prep, "prepare_tv_source", cache)
    row = worker.claim()
    thread = threading.Thread(target=worker.prepare_subtitles, args=(row,))
    thread.start()
    try:
        assert started.wait(timeout=5), "cache was not started"
        # Cache is still blocked, but the finished video is already published.
        song = store.db.execute(
            "SELECT prep_state,media_file FROM songs WHERE id=?", (song_id,)
        ).fetchone()
        assert song["prep_state"] == "done"
        assert (store.media / song["media_file"]).read_bytes() == b"BAKED"
        assert thread.is_alive()
        # A ready song must expose its subtitle archive before TV caching ends.
        current = metadata(store, song_id)
        assert current["outcome"] == "rendered"
        assert current["source_file"] == "original.mp4"
        assert "tv_cache_seconds" not in current
        media = MediaService(store, None, None, None)
        row = dict(
            store.db.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
        )
        assert media.overlay_bundle(row) is not None
        time.sleep(0.04)
    finally:
        resume.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    info = metadata(store, song_id)
    assert info["outcome"] == "rendered"
    assert info["tv_source_ready"] is True
    assert info["ready_seconds"] < info["elapsed_seconds"]
    assert info["tv_cache_seconds"] >= 0.04
    assert (store.media / f"{song_id}-auto-alignment").exists()


def test_cache_failure_keeps_published_song(tmp_path, monkeypatch):
    worker, store, song_id = prepared(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(prep, "prepare_tv_source", fail)
    worker.prepare_subtitles(worker.claim())
    song = store.db.execute(
        "SELECT prep_state,media_file FROM songs WHERE id=?", (song_id,)
    ).fetchone()
    assert song["prep_state"] == "done"
    assert (store.media / song["media_file"]).read_bytes() == b"BAKED"
    info = metadata(store, song_id)
    assert info["outcome"] == "rendered"
    assert info["tv_source_ready"] is False


def test_stale_render_does_not_build_cache(tmp_path, monkeypatch):
    worker, store, song_id = prepared(tmp_path)

    def render_and_invalidate(
        source, lyrics, destination, archive, duration, *, preset
    ):
        destination.write_bytes(b"BAKED")
        (archive / "aligned.lrc").write_text("lyrics")
        with store.lock:
            store.db.execute(
                "UPDATE songs SET prep_state='original' WHERE id=?", (song_id,)
            )
            store.db.commit()
        return {"rendered": True}

    def no_cache(*args, **kwargs):
        pytest.fail("stale render must not trigger cache")

    worker.enhance = render_and_invalidate
    monkeypatch.setattr(prep, "prepare_tv_source", no_cache)
    original_publish = prep.publish_subtitles

    def check_no_premature_manifest(*args, **kwargs):
        # An unpublished/stale attempt must not appear as rendered.
        assert not list(
            (store.media / f"{song_id}-auto-alignment").glob("attempt-*/attempt.json")
        )
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(prep, "publish_subtitles", check_no_premature_manifest)
    worker.prepare_subtitles(worker.claim())
    assert metadata(store, song_id)["outcome"] == "stale"
