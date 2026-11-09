"""Stale, cancelled and changed songs cannot publish newly prepared media."""

import json

import pytest

from tests.test_autoprep_source_publish_cleanup import pending_source
from tests.test_stage_durations import ready_song
from vc_ktv.processing.worker import AutoPrep


@pytest.mark.parametrize("during_copy", ["cancelled", "skip_subtitles"])
def test_original_publish_uses_current_song_state(tmp_path, monkeypatch, during_copy):
    store, worker, claimed, song_id = pending_source(tmp_path)
    from vc_ktv.processing import worker as worker_module

    original_check = worker_module.check_current_budget
    updated = False

    def update_during_copy():
        nonlocal updated
        original_check()
        # The publication helper invokes the original worker callback as it copies.
        if not updated and list(store.media.glob(f"{song_id}-original-*.mp4")):
            with store.lock:
                if during_copy == "cancelled":
                    store.db.execute(
                        "UPDATE songs SET status='cancelled' WHERE id=?", (song_id,)
                    )
                else:
                    store.db.execute(
                        "UPDATE songs SET skip_subtitles=1 WHERE id=?", (song_id,)
                    )
                store.db.commit()
            updated = True

    monkeypatch.setattr(worker_module, "check_current_budget", update_during_copy)
    worker.prepare_source(claimed)
    assert updated
    row = store.rows(
        "SELECT status,prep_state,skip_subtitles,media_file,source_file FROM songs WHERE id=?",
        (song_id,),
    )[0]
    if during_copy == "cancelled":
        assert row == {
            "status": "cancelled",
            "prep_state": "downloading",
            "skip_subtitles": 0,
            "media_file": None,
            "source_file": None,
        }
        assert list(store.media.iterdir()) == []
    else:
        assert row["status"] == "ready"
        assert row["prep_state"] == "original"
        assert row["skip_subtitles"] == 1
        assert row["media_file"] == row["source_file"]
        assert (store.media / row["source_file"]).is_file()
    assert not store.db.in_transaction


@pytest.mark.parametrize("change", ["cancelled", "playing", "lyrics"])
def test_subtitle_publish_discards_outdated_render(tmp_path, change):
    store, source, song_id = ready_song(tmp_path)

    def render(video, lyrics, target, archive, duration, *, preset):
        target.write_bytes(b"must never publish")
        (archive / "aligned.lrc").write_text("[00:01.00]changed lyrics")
        with store.lock:
            if change == "lyrics":
                store.db.execute(
                    "UPDATE songs SET lyric_choice='another lyric selection' WHERE id=?",
                    (song_id,),
                )
            else:
                store.db.execute(
                    "UPDATE songs SET status=? WHERE id=?", (change, song_id)
                )
            store.db.commit()
        return {"rendered": True}

    worker = AutoPrep(store, lambda path: 4, enhance=render)
    claimed = worker.claim()
    assert claimed and claimed["id"] == song_id
    worker.prepare_subtitles(claimed)

    row = store.rows(
        "SELECT status,prep_state,media_file,source_file FROM songs WHERE id=?",
        (song_id,),
    )[0]
    assert row["media_file"] == source.name
    assert row["source_file"] == source.name
    assert source.read_bytes() == b"unchanged-source"
    assert not list(store.media.glob(f"{song_id}-auto-*.mp4"))
    attempts = list(
        store.media.glob(f"{song_id}-auto-alignment/attempt-*/attempt.json")
    )
    assert len(attempts) == 1
    assert json.loads(attempts[0].read_text())["outcome"] == "stale"
    assert not store.db.in_transaction
