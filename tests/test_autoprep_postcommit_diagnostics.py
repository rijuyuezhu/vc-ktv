"""A diagnostic write failure must not undo a committed subtitle render."""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from tests.test_stage_durations import ready_song
from vc_ktv.processing.autoprep import AutoPrep


def eventually(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("fault", ("timing_update", "timing_commit", "attempt_report"))
@pytest.mark.parametrize("workers", (1, 4))
def test_successful_render_survives_post_commit_diagnostic_fault(
    tmp_path, monkeypatch, fault, workers
):
    store, source, song_id = ready_song(tmp_path)
    completed = threading.Event()

    def fake_render(video, lyrics, target, archive, duration, *, preset):
        assert video == source
        target.write_bytes(b"successfully rendered")
        (archive / "aligned.lrc").write_text("[00:00.20]real subtitle")
        completed.set()
        return {"rendered": True}

    worker = AutoPrep(store, lambda p: 4, enhance=fake_render, max_workers=workers)
    real_db = store.db

    class DiagnosticsDb:
        failures = 0
        rollbacks = 0
        pending_commit = False

        def execute(self, sql, *args):
            is_timing = "UPDATE songs SET prep_timing=?" in sql
            if fault == "timing_update" and is_timing and not self.failures:
                self.failures += 1
                raise sqlite3.OperationalError("diagnostics UPDATE fault")
            result = real_db.execute(sql, *args)
            if fault == "timing_commit" and is_timing and not self.failures:
                self.pending_commit = True
            return result

        def commit(self):
            if self.pending_commit and not self.failures:
                self.failures += 1
                self.pending_commit = False
                raise sqlite3.OperationalError("diagnostics COMMIT fault")
            return real_db.commit()

        def rollback(self):
            self.rollbacks += 1
            return real_db.rollback()

        def __getattr__(self, name):
            return getattr(real_db, name)

    wrapped = DiagnosticsDb()
    if fault != "attempt_report":
        store.db = wrapped
    else:
        real_write = Path.write_text

        def deny_attempt_report(path, *args, **kwargs):
            if path.name == "attempt.json":
                wrapped.failures += 1
                raise PermissionError("diagnostics JSON write fault")
            return real_write(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", deny_attempt_report)

    worker.start()
    try:
        assert completed.wait(4)
        assert eventually(
            lambda: worker.active_claims == 0 and not worker.busy.is_set()
        ), "preparation did not complete its bookkeeping"
        with store.lock:
            song = real_db.execute(
                "SELECT prep_state,prep_stage,prep_error,media_file,lyrics FROM songs WHERE id=?",
                (song_id,),
            ).fetchone()
            assert song["prep_state"] == "done"
            assert song["prep_stage"] == "done"
            assert song["prep_error"] == ""
            assert song["lyrics"] == "[00:00.20]real subtitle"
            assert (
                store.media / song["media_file"]
            ).read_bytes() == b"successfully rendered"
            assert not real_db.in_transaction
            assert wrapped.failures == 1
            if fault in ("timing_update", "timing_commit"):
                assert wrapped.rollbacks == 1
        assert len(worker.workers) == workers
        assert all(thread.is_alive() for thread in worker.workers)
    finally:
        worker.stop()
        store.db = real_db
