"""An optional per-stage progress DB failure must not abort subtitle rendering."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.test_stage_durations import ready_song
from vc_ktv import autoprep


@pytest.mark.parametrize("fault", ("stage_update", "stage_commit"))
@pytest.mark.parametrize("repeat_stage", (False, True))
def test_progress_telemetry_sqlite_failure_preserves_render(
    tmp_path, monkeypatch, fault, repeat_stage
):
    store, source, song_id = ready_song(tmp_path)
    callback_messages = []

    def fake_pipeline(
        video, lyrics, folder, settings, *, style, mode, render_options, progress
    ):
        for name in (
            "读取原视频并提取音频",
            "Kim 分离用于推理的人声",
            "将字幕烧录到保留原始音轨的 MP4",
        ):
            callback_messages.append(name)
            progress(name)
            if repeat_stage:
                progress(name)
        bundle = folder / "subtitles"
        bundle.mkdir(parents=True)
        (bundle / "lyrics.ass").write_text("ASS")
        (bundle / "lyrics.lrc").write_text("[00:00.20]一行歌词")
        (bundle / "export_report.json").write_text('{"rendered_lines":1}')
        (bundle / "karaoke.mp4").write_bytes(b"success-video")
        return SimpleNamespace(
            video=bundle / "karaoke.mp4",
            ass=bundle / "lyrics.ass",
            report=bundle / "export_report.json",
        )

    monkeypatch.setattr(autoprep, "make_karaoke_video", fake_pipeline)
    worker = autoprep.AutoPrep(store, lambda _: 4, max_workers=1)
    row = worker.claim()
    assert row and row["id"] == song_id
    real_db = store.db

    class FaultyProgress:
        failures = 0
        rollbacks = 0
        fail_next_commit = False

        def execute(self, sql, *args):
            is_progress = (
                "UPDATE songs SET prep_stage=?,prep_stage_at=?,prep_timing=?" in sql
            )
            if is_progress and fault == "stage_update" and not self.failures:
                self.failures += 1
                raise sqlite3.OperationalError("simulated stage progress UPDATE fault")
            result = real_db.execute(sql, *args)
            if is_progress and fault == "stage_commit" and not self.failures:
                self.fail_next_commit = True
            return result

        def commit(self):
            if self.fail_next_commit and not self.failures:
                self.failures += 1
                self.fail_next_commit = False
                raise sqlite3.OperationalError("simulated stage progress COMMIT fault")
            return real_db.commit()

        def rollback(self):
            self.rollbacks += 1
            return real_db.rollback()

        def __getattr__(self, name):
            return getattr(real_db, name)

    injected = FaultyProgress()
    store.db = injected
    try:
        worker.prepare_subtitles(row)
    finally:
        store.db = real_db

    saved = real_db.execute(
        "SELECT prep_state,prep_error,lyrics,media_file,prep_timing FROM songs WHERE id=?",
        (song_id,),
    ).fetchone()
    assert len(callback_messages) == 3
    assert injected.failures == 1
    assert injected.rollbacks == 1
    assert not real_db.in_transaction
    assert saved["prep_state"] == "done"
    assert saved["prep_error"] == ""
    assert saved["lyrics"] == "[00:00.20]一行歌词"
    assert (store.media / saved["media_file"]).read_bytes() == b"success-video"
    assert source.read_bytes() == b"unchanged-source"
    assert "render" in json.loads(saved["prep_timing"])
    reports = list(
        (store.media / f"{song_id}-auto-alignment").glob("attempt-*/attempt.json")
    )
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["outcome"] == "rendered"
