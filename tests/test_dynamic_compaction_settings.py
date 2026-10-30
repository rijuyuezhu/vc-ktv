"""Opt-in second-pass encoding, effective on the next subtitle job claim."""

import json
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

from vc_ktv import autoprep
from vc_ktv.app import create_app


def make_app(tmp_path):
    return create_app(
        root=tmp_path / "ktv",
        password="test-admin",
        secret=b"compression-settings-test",
        auto_prepare=False,
    )


def login(client):
    assert (
        client.post("/api/admin/login", json={"password": "test-admin"}).status_code
        == 200
    )


def update(client, **overrides):
    return client.put(
        "/api/admin/settings",
        json={"queue_policy": "fair", "allow_unsubtitled": False, **overrides},
    )


def test_compression_admin_only_default_off_persists_and_resets_between_events(
    tmp_path,
):
    app = make_app(tmp_path)
    first = app.state.events.create("First", "000001")
    with TestClient(app) as owner, TestClient(app) as guest:
        login(owner)
        assert owner.get("/api/admin/settings").json()["compact_video"] is False
        assert guest.get("/api/admin/settings").status_code == 401
        assert update(guest, compact_video=True).status_code == 401
        result = update(owner, compact_video=True)
        assert result.status_code == 200
        assert result.json()["compact_video"] is True
        # Legacy clients with no compact_video field must preserve the setting.
        assert (
            update(owner, subtitle_style="large", video_quality=480).status_code == 200
        )
        assert owner.get("/api/admin/settings").json()["compact_video"] is True
        assert update(owner, compact_video="definitely").status_code == 422
        assert update(owner, compact_video=False).json()["compact_video"] is False
        assert update(owner, compact_video=True).json()["compact_video"] is True
        with sqlite3.connect(
            tmp_path / "ktv" / "sessions" / first["id"] / "room.sqlite3"
        ) as db:
            assert (
                db.execute(
                    "SELECT value FROM room_state WHERE name='compact_video'"
                ).fetchone()[0]
                == "1"
            )
        assert owner.post("/api/admin/events/close").status_code == 200
        assert (
            owner.post("/api/admin/events", json={"name": "Second"}).status_code == 200
        )
        assert owner.get("/api/admin/settings").json()["compact_video"] is False
    restarted = make_app(tmp_path)
    assert restarted.state.store.settings()["compact_video"] is False
    with sqlite3.connect(
        tmp_path / "ktv" / "sessions" / first["id"] / "room.sqlite3"
    ) as db:
        assert (
            db.execute(
                "SELECT value FROM room_state WHERE name='compact_video'"
            ).fetchone()[0]
            == "1"
        )


def test_compression_toggle_snapshotted_per_subtitle_attempt_and_forwarded(
    tmp_path, monkeypatch
):
    app = make_app(tmp_path)
    app.state.events.create("First", "000001")
    store = app.state.store
    original = store.media / "original.mp4"
    original.write_bytes(b"source untouched")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES ('owner','Owner')")
        for n in (1, 2, 3):
            store.db.execute(
                "INSERT INTO songs(bvid,page,title,uploader,cover,duration,"
                "requester,requested_at,status,media_file,source_file,"
                "prep_state,lyric_choice,lyric_source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "BV0000000001",
                    n,
                    f"song-{n}",
                    "up",
                    "",
                    5,
                    "owner",
                    n,
                    "ready",
                    original.name,
                    original.name,
                    "downloaded",
                    "这是一句完整的歌词",
                    "LRCLIB",
                ),
            )
        store.db.commit()

    received = []

    def fake_pipeline(
        video, lyrics, folder, settings, *, style, mode, render_options, progress
    ):
        received.append(render_options.compact_video)
        assert style.mode in ("double", "single")
        assert mode == "quality"
        assert video == original.resolve()
        result = folder / "subtitles"
        result.mkdir(parents=True)
        (result / "lyrics.lrc").write_text("[00:01.00]这是一句完整的歌词")
        (result / "lyrics.ass").write_text("ASS")
        (result / "export_report.json").write_text('{"rendered_lines": 1}')
        (result / "karaoke.mp4").write_bytes(b"captioned")
        return SimpleNamespace(
            video=result / "karaoke.mp4",
            ass=result / "lyrics.ass",
            report=result / "export_report.json",
        )

    monkeypatch.setattr(autoprep, "make_karaoke_video", fake_pipeline)
    worker = autoprep.AutoPrep(store, lambda path: 5)
    with TestClient(app) as owner:
        login(owner)
        first = worker.claim()
        assert first["compact_video"] is False
        assert update(owner, compact_video=True).status_code == 200
        # Changing the setting does not mutate the already claimed attempt.
        worker.prepare_subtitles(first)
        worker.busy.clear()
        second = worker.claim()
        assert second["compact_video"] is True
        assert update(owner, compact_video=False).status_code == 200
        worker.prepare_subtitles(second)
        worker.busy.clear()
        third = worker.claim()
        assert third["compact_video"] is False
        worker.prepare_subtitles(third)
    assert received == [False, True, False]
    assert original.read_bytes() == b"source untouched"
    for row, expected in ((first, False), (second, True), (third, False)):
        archive = next((store.media / f"{row['id']}-auto-alignment").glob("attempt-*"))
        assert (
            json.loads((archive / "attempt.json").read_text())["compact_video"]
            is expected
        )
        assert (
            store.db.execute(
                "SELECT prep_state FROM songs WHERE id=?", (row["id"],)
            ).fetchone()[0]
            == "done"
        )
