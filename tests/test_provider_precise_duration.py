"""Regression for provider LRC in the fractional tail of real media duration.

The room stores rounded seconds; subtitle timing must use FFprobe's duration.
"""

import subprocess

import pytest

import vc_ktv.processing.worker as preparation
from vc_ktv.core.store import Store
from vc_ktv.lyrics.timed_lyrics import offset_timeline
from vc_ktv.processing.media_preparation import probe_media

TIMED = "[00:01.00]第一句歌词\n[00:03.00]最后一句歌词"


def source_video(tmp_path, duration=3.386):
    path = tmp_path / "media-source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return path


def setup_song(tmp_path, duration=3.386):
    store = Store(tmp_path / "room")
    src = source_video(tmp_path, duration)
    target_source = store.media / "original.mp4"
    target_source.write_bytes(src.read_bytes())
    actual = probe_media(target_source)
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('test','Guest')")
        song_id = store.db.execute(
            """INSERT INTO songs(
               bvid,page,title,uploader,cover,duration,requester,requested_at,
               status,prep_state,source_file,media_file,lyric_choice,
               lyric_source,lyric_timing_mode,provider_lrc,source_lrc_raw)
               VALUES ('BV0000000001',1,'test','uploader','',?,'test',1,
                       'ready','downloaded',?,?,'两句歌词','网易云音乐','provider',?,?)""",
            (round(actual), target_source.name, target_source.name, TIMED, TIMED),
        ).lastrowid
        store.db.commit()
    return store, song_id, target_source, actual


def test_last_lrc_stamp_within_fractional_tail_publishes(tmp_path, monkeypatch):
    store, song_id, source, actual = setup_song(tmp_path)
    assert round(actual) == 3
    assert 3.0 < actual < 4.0

    durations = []

    def render(src, timed, offset, destination, archive, duration, **kwargs):
        assert src == source
        assert timed == TIMED
        assert offset == 0
        durations.append(duration)
        aligned = offset_timeline(timed, offset, duration)
        (archive / "aligned.lrc").write_text(aligned)
        destination.write_bytes(b"BAKED")
        return {"mode": "provider_line_only"}

    monkeypatch.setattr(preparation, "render_provider_video", render)
    monkeypatch.setattr(preparation, "prepare_tv_source", lambda *a, **kw: None)
    worker = preparation.AutoPrep(store, probe_media, gpu_alignment=False)
    task = worker.claim("fast")
    assert task and task["id"] == song_id
    worker.prepare_subtitles(task)

    assert durations == pytest.approx([actual])
    row = store.db.execute(
        "SELECT prep_state,media_file,prep_error,duration FROM songs WHERE id=?",
        (song_id,),
    ).fetchone()
    assert row["prep_state"] == "done"
    assert row["duration"] == 3  # Preserve existing display/queue behavior.
    assert row["prep_error"] == ""
    assert (store.media / row["media_file"]).read_bytes() == b"BAKED"
    assert source.is_file()


def test_actual_out_of_range_lrc_is_still_rejected(tmp_path, monkeypatch):
    store, song_id, source, actual = setup_song(tmp_path, duration=2.85)
    assert actual < 3.0

    def render(src, timed, offset, destination, archive, duration, **kwargs):
        offset_timeline(timed, offset, duration)
        pytest.fail("Invalid provider timing must not render output")

    monkeypatch.setattr(preparation, "render_provider_video", render)
    worker = preparation.AutoPrep(store, probe_media, gpu_alignment=False)
    task = worker.claim("fast")
    assert task and task["id"] == song_id
    worker.prepare_subtitles(task)

    row = store.db.execute(
        "SELECT prep_state,media_file,prep_error,duration FROM songs WHERE id=?",
        (song_id,),
    ).fetchone()
    assert row["prep_state"] == "original"
    assert row["media_file"] == source.name
    assert source.is_file()
    assert not list(store.media.glob(f"{song_id}-auto-*.mp4"))


def test_fractional_tail_renders_real_burned_video(tmp_path, monkeypatch):
    """A real 3.4s FFmpeg render must not reject a last line at 3.00s."""
    store, song_id, source, actual = setup_song(tmp_path)
    assert 3.0 < actual < 3.5
    monkeypatch.setattr(preparation, "prepare_tv_source", lambda *a, **kw: None)
    worker = preparation.AutoPrep(store, probe_media, gpu_alignment=False)
    task = worker.claim("fast")
    assert task and task["id"] == song_id
    worker.prepare_subtitles(task)

    row = store.db.execute(
        "SELECT prep_state,media_file,prep_error FROM songs WHERE id=?",
        (song_id,),
    ).fetchone()
    assert row["prep_state"] == "done", row["prep_error"]
    baked = store.media / row["media_file"]
    assert baked.is_file() and baked.stat().st_size > 1024
    assert probe_media(baked) > 3.0
    archive = next((store.media / f"{song_id}-auto-alignment").glob("attempt-*"))
    aligned = (archive / "aligned.lrc").read_text()
    assert "[00:03.00]最后一句歌词" in aligned
