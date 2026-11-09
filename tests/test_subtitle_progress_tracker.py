"""Subtitle progress writes exactly on stage transitions, not every callback."""

import time

from tests.test_stage_durations import ready_song
from vc_ktv.processing.subtitle_progress import SubtitleProgress


def test_stage_tracker_ignores_unknown_and_duplicate_messages(tmp_path):
    store, _, song_id = ready_song(tmp_path)
    with store.lock:
        store.db.execute(
            "UPDATE songs SET prep_state='aligning' WHERE id=?", (song_id,)
        )
        store.db.commit()
    original = store.db

    class CountingDB:
        stage_writes = 0

        def execute(self, sql, *args):
            if "UPDATE songs SET prep_stage=?,prep_stage_at=?,prep_timing=?" in sql:
                self.stage_writes += 1
            return original.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(original, name)

    wrapped = CountingDB()
    store.db = wrapped
    try:
        progress = SubtitleProgress(
            store, song_id, {"download": 1.23}, time.monotonic()
        )
        progress("unknown stage")
        progress("读取原视频并提取音频")
        progress("读取原视频并提取音频")
        progress("Kim 分离用于推理的人声")
        progress("Kim 分离用于推理的人声")
        result = progress.finish()
        assert wrapped.stage_writes == 2
        assert {"download", "extract", "kim"} <= result.keys()

        provider = SubtitleProgress(store, song_id, {}, time.monotonic())
        provider.start_provider_render()
        assert wrapped.stage_writes == 2
        assert "render" in provider.finish()
    finally:
        store.db = original


def test_stage_tracker_zero_row_update_closes_transaction(tmp_path):
    store, _, song_id = ready_song(tmp_path)
    # Song is not aligning, so the guarded progress UPDATE affects no rows.
    assert not store.db.in_transaction
    progress = SubtitleProgress(store, song_id, {}, time.monotonic())
    progress("读取原视频并提取音频")
    assert not store.db.in_transaction
    assert (
        store.db.execute(
            "SELECT prep_stage FROM songs WHERE id=?", (song_id,)
        ).fetchone()[0]
        == ""
    )
    # Later updates must not accidentally inherit the progress transaction.
    with store.lock:
        store.db.execute(
            "UPDATE songs SET prep_state='aligning' WHERE id=?", (song_id,)
        )
        store.db.commit()
    progress("Kim 分离用于推理的人声")
    assert not store.db.in_transaction
    assert (
        store.db.execute(
            "SELECT prep_stage FROM songs WHERE id=?", (song_id,)
        ).fetchone()[0]
        == "kim"
    )
