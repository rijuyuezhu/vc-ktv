"""A failed SQLite commit must not publish a deleted subtitle video."""

import sqlite3

import pytest

from tests.test_stage_durations import ready_song
from vc_ktv.processing.autoprep import AutoPrep


@pytest.mark.parametrize("prior_baked", [False, True])
def test_render_publish_commit_failure_preserves_original_media(tmp_path, prior_baked):
    store, source, song_id = ready_song(tmp_path)
    prior = source
    if prior_baked:
        prior = store.media / "old-baked.mp4"
        prior.write_bytes(b"previously completed karaoke")
        with store.lock:
            store.db.execute(
                "UPDATE songs SET media_file=? WHERE id=?", (prior.name, song_id)
            )
            store.db.commit()

    def render(video, lyrics, destination, archive, duration, *, preset):
        destination.write_bytes(b"new karaoke render")
        (archive / "aligned.lrc").write_text("[00:01.00]finished", encoding="utf-8")
        return {"rendered": True}

    worker = AutoPrep(store, lambda path: 4, enhance=render)
    claimed = worker.claim()
    assert claimed and claimed["id"] == song_id
    underlying = store.db

    class FailPublicationCommit:
        fail_next_commit = False
        did_fail = False

        def execute(self, sql, args=()):
            if sql.startswith("UPDATE songs SET media_file="):
                self.fail_next_commit = True
            return underlying.execute(sql, args)

        def commit(self):
            if self.fail_next_commit and not self.did_fail:
                self.did_fail = True
                self.fail_next_commit = False
                raise sqlite3.OperationalError(
                    "simulated subtitle publication commit failure"
                )
            return underlying.commit()

        def __getattr__(self, name):
            return getattr(underlying, name)

    injected = FailPublicationCommit()
    store.db = injected
    worker.prepare_subtitles(claimed)
    assert injected.did_fail

    row = store.rows(
        "SELECT status,prep_state,media_file,prep_error FROM songs WHERE id=?",
        (song_id,),
    )[0]
    assert row["status"] == "ready"
    assert row["prep_state"] == ("done" if prior_baked else "original")
    assert row["media_file"] == prior.name
    assert prior.is_file()
    assert source.read_bytes() == b"unchanged-source"
    assert not list(store.media.glob(f"{song_id}-auto-*.mp4"))
    assert not underlying.in_transaction
