"""Failures after rendering must not delete an already-published video."""

from pathlib import Path

import vc_ktv.processing.worker as worker_module
from tests.test_publish_before_tv_cache import prepared


def test_cache_cleanup_fault_preserves_committed_video(tmp_path, monkeypatch):
    worker, store, song_id = prepared(tmp_path)

    def fail_cache(*args, **kwargs):
        raise RuntimeError("cache transcode failed")

    original_unlink = Path.unlink

    def fail_cleanup(path, *args, **kwargs):
        if path.name == "tv-source.mp4":
            raise PermissionError("cache cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(worker_module, "prepare_tv_source", fail_cache)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    worker.prepare_subtitles(worker.claim())
    row = store.rows(
        "SELECT prep_state,media_file,prep_error FROM songs WHERE id=?", (song_id,)
    )[0]
    assert row["prep_state"] == "done"
    assert row["prep_error"] == ""
    assert (store.media / row["media_file"]).is_file()


def test_postpublication_metadata_failure_preserves_committed_video(
    tmp_path, monkeypatch
):
    worker, store, song_id = prepared(tmp_path)

    def cache_noop(*args, **kwargs):
        return None

    original_is_file = Path.is_file

    def fail_cache_stat(path):
        if path.name == "tv-source.mp4":
            raise OSError("cache directory transiently unavailable")
        return original_is_file(path)

    monkeypatch.setattr(worker_module, "prepare_tv_source", cache_noop)
    monkeypatch.setattr(Path, "is_file", fail_cache_stat)
    worker.prepare_subtitles(worker.claim())
    row = store.rows(
        "SELECT prep_state,media_file,prep_error FROM songs WHERE id=?", (song_id,)
    )[0]
    assert row["prep_state"] == "done"
    assert row["prep_error"] == ""
    assert (store.media / row["media_file"]).is_file()
