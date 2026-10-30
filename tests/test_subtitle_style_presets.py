"""Only the current event administrator may select new karaoke render styles."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vc_ktv.app import Store, create_app
from vc_ktv.autoprep import AutoPrep, render_upstream_video
from vc_ktv.subtitle_styles import preset_style


@pytest.mark.parametrize(
    ("name", "mode", "size", "position", "outline"),
    [
        ("classic", "double", 48, 0.86, 3),
        ("large", "double", 62, 0.84, 5),
        ("compact", "single", 38, 0.91, 2),
    ],
)
def test_style_presets_use_upstream_model(name, mode, size, position, outline):
    style = preset_style(name)
    assert (style.mode, style.size, style.position, style.outline) == (
        mode,
        size,
        position,
        outline,
    )
    assert style.auto_complete and style.font == "Noto Sans CJK SC"


def test_admin_only_settings_validation_per_event_and_restart(tmp_path):
    root = tmp_path / "data"
    app = create_app(
        root=root,
        password="test-password",
        secret=b"test-preset-secret",
        auto_prepare=False,
    )
    first = app.state.events.create("First", "000001")
    with TestClient(app) as guest, TestClient(app) as admin:
        payload = {
            "queue_policy": "fair",
            "allow_unsubtitled": False,
            "subtitle_style": "large",
        }
        assert guest.get("/api/admin/settings").status_code == 401
        assert guest.put("/api/admin/settings", json=payload).status_code == 401
        assert (
            admin.post(
                "/api/admin/login", json={"password": "test-password"}
            ).status_code
            == 200
        )
        assert admin.get("/api/admin/settings").json()["subtitle_style"] == "classic"
        assert (
            admin.put(
                "/api/admin/settings",
                json={
                    **payload,
                    "subtitle_style": "other",
                },
            ).status_code
            == 422
        )
        assert admin.put("/api/admin/settings", json=payload).status_code == 200
        assert admin.get("/api/admin/settings").json()["subtitle_style"] == "large"
        # Old browser clients can still update queue policy without resetting style.
        assert (
            admin.put(
                "/api/admin/settings",
                json={"queue_policy": "fcfs", "allow_unsubtitled": False},
            ).status_code
            == 200
        )
        assert admin.get("/api/admin/settings").json()["subtitle_style"] == "large"
        assert admin.post("/api/admin/events/close").status_code == 200
        second = admin.post(
            "/api/admin/events", json={"name": "Second", "code": "000002"}
        ).json()
        assert admin.get("/api/admin/settings").json()["subtitle_style"] == "classic"
        assert first["id"] != second["id"]
    reopened = create_app(
        root=root,
        password="test-password",
        secret=b"test-preset-secret",
        auto_prepare=False,
    )
    assert reopened.state.store.settings()["subtitle_style"] == "classic"
    old_db = root / "sessions" / first["id"] / "room.sqlite3"
    import sqlite3

    with sqlite3.connect(old_db) as db:
        assert (
            db.execute(
                "SELECT value FROM room_state WHERE name='subtitle_style'"
            ).fetchone()[0]
            == "large"
        )


def test_style_is_snapshotted_at_claim_and_retry_uses_new_style(tmp_path):
    store = Store(tmp_path / "room")
    source = store.media / "original.mp4"
    source.write_bytes(b"original-video-audio")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES ('owner','Owner')")
        for n in (1, 2):
            store.db.execute(
                "INSERT INTO songs(bvid,page,title,uploader,cover,duration,"
                "requester,requested_at,status,media_file,source_file,"
                "prep_state,lyric_choice,lyric_source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "BV0000000001",
                    n,
                    "song",
                    "owner",
                    "",
                    5,
                    "owner",
                    n,
                    "ready",
                    source.name,
                    source.name,
                    "downloaded",
                    "逐字歌词第一句\n逐字歌词第二句",
                    "LRCLIB",
                ),
            )
        store.save_settings("fair", False, "large")

    passed_styles = []

    def render(video, lyrics, output, folder, duration, *, preset):
        passed_styles.append(preset)
        (folder / "aligned.lrc").write_text("[00:01.00]逐字歌词")
        output.write_bytes(video.read_bytes())
        return {"rendered": True}

    worker = AutoPrep(store, lambda path: 5, enhance=render)
    try:
        row1 = worker.claim()
        assert row1["style_preset"] == "large"
        store.save_settings("fair", False, "compact")
        worker.prepare_subtitles(row1)
        worker.busy.clear()
        row2 = worker.claim()
        assert row2["style_preset"] == "compact"
        worker.prepare_subtitles(row2)
        assert passed_styles == ["large", "compact"]
        for row, expected in ((row1, "large"), (row2, "compact")):
            assert (
                store.db.execute(
                    "SELECT prep_state FROM songs WHERE id=?", (row["id"],)
                ).fetchone()[0]
                == "done"
            )
            attempt = next(
                (store.media / f"{row['id']}-auto-alignment").glob("attempt-*")
            )
            assert (
                json.loads((attempt / "attempt.json").read_text())["subtitle_style"]
                == expected
            )
    finally:
        store.db.close()


def test_real_adapter_forwards_selected_style(tmp_path, monkeypatch):
    from vc_ktv import autoprep

    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    work = tmp_path / "archive"
    work.mkdir()

    def fake_pipeline(video, lyrics, dest, settings, *, style, mode, render_options):
        assert mode == "global-kim"
        assert render_options.compact_video is False
        assert video == source
        assert lyrics == "第一句\n第二句"
        assert style.mode == "single" and style.size == 38
        subtitles = dest / "subtitles"
        subtitles.mkdir(parents=True)
        ass = subtitles / "lyrics.ass"
        ass.write_text("ASS")
        (subtitles / "lyrics.lrc").write_text("[00:01.00]第一句")
        report = subtitles / "export_report.json"
        report.write_text('{"rendered_lines":1}')
        result = subtitles / "karaoke.mp4"
        result.write_bytes(b"finished")
        return SimpleNamespace(video=result, ass=ass, report=report)

    monkeypatch.setattr(autoprep, "make_karaoke_video", fake_pipeline)
    dest = tmp_path / "output.mp4"
    assert (
        render_upstream_video(
            source, "第一句\n第二句", dest, work, 5, preset="compact"
        )["rendered_lines"]
        == 1
    )
    assert dest.read_bytes() == b"finished"
    assert source.read_bytes() == b"original"


@pytest.mark.parametrize("succeeds", [True, False])
def test_restyle_completed_song_preserves_old_until_success(tmp_path, succeeds):
    root = tmp_path / "room"
    app = create_app(
        root=root,
        password="test-password",
        secret=b"test-preset-secret",
        auto_prepare=False,
    )
    app.state.events.create("Restyle", "000001")
    store = app.state.store
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES ('u','Guest')")
        original = store.media / "input.mp4"
        previous = store.media / "finished.mp4"
        original.write_bytes(b"ORIGINAL-AUDIO")
        previous.write_bytes(b"OLD-VIDEO")
        cursor = store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,"
            "requester,requested_at,status,media_file,source_file,prep_state,"
            "lyric_choice,lyric_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "BV0000000001",
                1,
                "Song",
                "Guest",
                "",
                5,
                "u",
                1,
                "ready",
                previous.name,
                original.name,
                "done",
                "第一句歌词\n第二句歌词",
                "LRCLIB",
            ),
        )
        sid = cursor.lastrowid
        store.db.commit()

    attempts = []

    def enhance(source, text, destination, folder, duration, *, preset):
        attempts.append(preset)
        assert source == original
        assert previous.read_bytes() == b"OLD-VIDEO"
        if not succeeds:
            raise RuntimeError("SIMULATED FFMPEG FAILURE")
        destination.write_bytes(b"NEW-VIDEO")
        (folder / "aligned.lrc").write_text("[00:01.00]第一句歌词")
        return {"rendered": True}

    with TestClient(app) as guest, TestClient(app) as admin:
        assert guest.post(f"/api/admin/songs/{sid}/subtitles/retry").status_code == 401
        assert (
            admin.post(
                "/api/admin/login", json={"password": "test-password"}
            ).status_code
            == 200
        )
        assert (
            admin.put(
                "/api/admin/settings",
                json={
                    "queue_policy": "fair",
                    "allow_unsubtitled": False,
                    "subtitle_style": "compact",
                },
            ).status_code
            == 200
        )
        assert admin.post(f"/api/admin/songs/{sid}/subtitles/retry").status_code == 200
        assert admin.post(f"/api/admin/songs/{sid}/subtitles/retry").status_code == 409
        assert previous.is_file()
        assert (
            store.db.execute(
                "SELECT media_file FROM songs WHERE id=?", (sid,)
            ).fetchone()[0]
            == previous.name
        )
        worker = app.state.autoprep
        worker.enhance = enhance
        row = worker.claim()
        assert row["style_preset"] == "compact"
        worker.prepare_subtitles(row)
        media, prep = store.db.execute(
            "SELECT media_file,prep_state FROM songs WHERE id=?", (sid,)
        ).fetchone()
        assert attempts == ["compact"]
        assert prep == "done"
        if succeeds:
            assert media != previous.name
            assert (store.media / media).read_bytes() == b"NEW-VIDEO"
            assert not previous.exists()
        else:
            assert media == previous.name
            assert previous.read_bytes() == b"OLD-VIDEO"
        assert original.read_bytes() == b"ORIGINAL-AUDIO"
        with store.lock:
            store.db.execute("UPDATE songs SET status='playing' WHERE id=?", (sid,))
            store.db.commit()
        assert admin.post(f"/api/admin/songs/{sid}/subtitles/retry").status_code == 409
