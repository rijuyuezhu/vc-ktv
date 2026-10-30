"""Real-stage timing diagnostics survive successful and failed subtitle attempts."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

from vc_ktv import autoprep
from vc_ktv.app import Store


def ready_song(tmp_path):
    store = Store(tmp_path / "room")
    source = store.media / "original.mp4"
    source.write_bytes(b"unchanged-source")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('a','A')")
        song_id = store.db.execute(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,
                status,source_file,media_file,prep_state,lyric_choice)
               VALUES('BV0000000001',1,'歌曲','UP','',4,'a',1,'ready',?,?,'downloaded','一行歌词')""",
            (source.name, source.name),
        ).lastrowid
        store.db.execute(
            "UPDATE songs SET prep_timing=? WHERE id=?",
            (json.dumps({"download": 1.23}), song_id),
        )
        store.db.commit()
    return store, source, song_id


def test_successful_full_song_tifa_records_each_real_stage(tmp_path, monkeypatch):
    store, source, song_id = ready_song(tmp_path)
    messages = [
        "读取原视频并提取音频",
        "Kim 分离用于推理的人声",
        "整首 TIFA 对齐完整歌词",
        "生成 ASS、LRC 与时间轴记录",
        "将字幕烧录到保留原始音轨的 MP4",
    ]

    def fake_pipeline(
        video, lyrics, folder, settings, *, style, mode, render_options, progress
    ):
        for message in messages:
            progress(message)
            time.sleep(0.012)
        bundle = folder / "subtitles"
        bundle.mkdir(parents=True)
        (bundle / "lyrics.ass").write_text("ASS")
        (bundle / "lyrics.lrc").write_text("[00:00.20]一行歌词")
        (bundle / "export_report.json").write_text('{"rendered_lines":1}')
        (bundle / "karaoke.mp4").write_bytes(b"output-mp4")
        return SimpleNamespace(
            video=bundle / "karaoke.mp4",
            ass=bundle / "lyrics.ass",
            report=bundle / "export_report.json",
        )

    monkeypatch.setattr(autoprep, "make_karaoke_video", fake_pipeline)
    worker = autoprep.AutoPrep(store, lambda path: 4)
    row = worker.claim()
    assert row["id"] == song_id
    worker.prepare_subtitles(row)
    data = store.db.execute(
        "SELECT prep_timing,prep_state FROM songs WHERE id=?", (song_id,)
    ).fetchone()
    timing = json.loads(data["prep_timing"])
    assert data["prep_state"] == "done"
    assert timing["download"] == 1.23
    assert all(
        timing[k] > 0.0 for k in ("extract", "kim", "align", "subtitles", "render")
    )
    attempt = next(
        (store.media / f"{song_id}-auto-alignment").glob("attempt-*/attempt.json")
    )
    report = json.loads(attempt.read_text())
    assert report["outcome"] == "rendered"
    assert report["stage_seconds"] == timing
    assert (
        report["elapsed_seconds"]
        >= sum(timing[k] for k in ("extract", "kim", "align", "subtitles", "render"))
        - 0.03
    )
    assert source.read_bytes() == b"unchanged-source"


def test_failed_pipeline_retains_completed_stage_durations(tmp_path, monkeypatch):
    store, source, song_id = ready_song(tmp_path)

    def fail_pipeline(
        video, lyrics, folder, settings, *, style, mode, render_options, progress
    ):
        progress("读取原视频并提取音频")
        time.sleep(0.012)
        progress("Kim 分离用于推理的人声")
        time.sleep(0.012)
        progress("整首 TIFA 对齐完整歌词")
        time.sleep(0.012)
        raise RuntimeError("model cannot align")

    monkeypatch.setattr(autoprep, "make_karaoke_video", fail_pipeline)
    worker = autoprep.AutoPrep(store, lambda path: 4)
    worker.prepare_subtitles(worker.claim())
    row = store.db.execute(
        "SELECT prep_state,prep_timing FROM songs WHERE id=?", (song_id,)
    ).fetchone()
    assert row["prep_state"] == "original"
    durations = json.loads(row["prep_timing"])
    assert durations["download"] == 1.23
    assert all(durations[k] > 0 for k in ("extract", "kim", "align"))
    assert "render" not in durations
    attempt = next(
        (store.media / f"{song_id}-auto-alignment").glob("attempt-*/attempt.json")
    )
    assert json.loads(attempt.read_text())["outcome"] == "failed"
    assert source.read_bytes() == b"unchanged-source"


def test_source_download_duration_persisted(tmp_path):
    store = Store(tmp_path / "room")
    store.db.execute("INSERT INTO guests(id,name) VALUES('a','A')")
    song_id = store.db.execute(
        """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at)
           VALUES('BV0000000001',1,'Song','UP','',2,'a',1)"""
    ).lastrowid
    store.db.commit()

    def fake_download(bvid, page, folder):
        p = Path(folder) / "song.mp4"
        p.write_bytes(b"source")
        return p

    worker = autoprep.AutoPrep(store, lambda p: 2, download=fake_download)
    worker.prepare_source(worker.claim())
    state = store.db.execute(
        "SELECT prep_state,prep_timing FROM songs WHERE id=?", (song_id,)
    ).fetchone()
    assert state["prep_state"] == "downloaded"
    assert "download" in json.loads(state["prep_timing"])
