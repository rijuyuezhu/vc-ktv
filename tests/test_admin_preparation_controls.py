"""Per-song admin bypass, cancellation and cheap coarse stage visibility."""

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from vc_ktv.app import create_app
from vc_ktv.autoprep import AutoPrep


def make_app(tmp_path):
    app = create_app(
        root=tmp_path / "ktv",
        password="test-admin",
        secret=b"admin-stage-secret",
        auto_prepare=False,
    )
    app.state.events.create("场次A", "000001")
    return app


def add_song(store, name, requested_at, *, state="downloaded", status="ready"):
    with store.lock:
        store.db.execute(
            "INSERT OR IGNORE INTO guests(id,name) VALUES(?,?)", (name, name)
        )
        file = store.media / f"original-{name}.mp4"
        file.write_bytes(b"source-video")
        cursor = store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,"
            "requester,requested_at,status,media_file,source_file,prep_state,"
            "lyric_choice,lyric_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "BV0000000001",
                1,
                name,
                "UP",
                "",
                5,
                name,
                requested_at,
                status,
                file.name,
                file.name,
                state,
                "歌词第一句\n歌词第二句",
                "LRCLIB",
            ),
        )
        store.db.commit()
        return cursor.lastrowid


def login(client):
    assert (
        client.post("/api/admin/login", json={"password": "test-admin"}).status_code
        == 200
    )


def test_single_song_skip_admin_auth_and_other_song_isolation(tmp_path):
    app = make_app(tmp_path)
    a = add_song(app.state.store, "A", 1)
    b = add_song(app.state.store, "B", 2)
    with TestClient(app) as admin, TestClient(app) as guest:
        url = f"/api/admin/songs/{a}/subtitles/skip"
        assert guest.post(url).status_code == 401
        login(admin)
        initial = admin.get("/api/admin/state").json()
        assert not initial["queue"]
        assert len(initial["waiting"]) == 2
        assert admin.post(url).status_code == 200
        after = admin.get("/api/admin/state").json()
        assert [s["id"] for s in after["queue"]] == [a]
        assert [s["id"] for s in after["waiting"]] == [b]
        assert after["queue"][0]["skip_subtitles"] == 1
        assert after["queue"][0]["prep_stage"] == "skipped"
        assert admin.get("/api/admin/settings").json()["allow_unsubtitled"] is False
        assert admin.post(url).status_code == 409
        assert admin.post(f"/api/admin/songs/{a}/subtitles/retry").status_code == 200
        recovered = admin.get("/api/admin/state").json()
        assert not recovered["queue"]
        assert {s["id"] for s in recovered["waiting"]} == {a, b}
        row = next(s for s in recovered["waiting"] if s["id"] == a)
        assert not row["skip_subtitles"]
        assert row["prep_stage"] == "queued"
        assert admin.post(url).status_code == 200


def test_skip_only_ready_with_actual_original_and_not_playing(tmp_path):
    app = make_app(tmp_path)
    ready = add_song(app.state.store, "Ready", 1, state="aligning")
    waiting = add_song(
        app.state.store, "Waiting", 2, status="waiting", state="downloading"
    )
    with TestClient(app) as admin:
        login(admin)
        assert (
            admin.post(f"/api/admin/songs/{waiting}/subtitles/skip").status_code == 409
        )
        with app.state.store.lock:
            app.state.store.db.execute(
                "UPDATE songs SET status='playing' WHERE id=?", (ready,)
            )
            app.state.store.db.commit()
        assert admin.post(f"/api/admin/songs/{ready}/subtitles/skip").status_code == 409
        with app.state.store.lock:
            app.state.store.db.execute(
                "UPDATE songs SET status='ready' WHERE id=?", (ready,)
            )
            app.state.store.db.commit()
        assert admin.post(f"/api/admin/songs/{ready}/subtitles/skip").status_code == 200


def test_skip_mid_alignment_keeps_original_even_after_stale_job(tmp_path):
    app = make_app(tmp_path)
    sid = add_song(app.state.store, "A", 1)
    store = app.state.store

    def fake_render(source, lyrics, destination, folder, duration, *, preset):
        destination.write_bytes(b"stale-subtitled-video")
        (folder / "aligned.lrc").write_text("old-lyrics")
        return {"rendered": True}

    worker = AutoPrep(store, lambda path: 5, enhance=fake_render)
    row = worker.claim()
    assert row["id"] == sid
    assert (
        store.db.execute("SELECT prep_state FROM songs WHERE id=?", (sid,)).fetchone()[
            0
        ]
        == "aligning"
    )
    with TestClient(app) as admin:
        login(admin)
        assert admin.post(f"/api/admin/songs/{sid}/subtitles/skip").status_code == 200
        worker.prepare_subtitles(row)
        saved = store.db.execute(
            "SELECT prep_state,media_file,skip_subtitles FROM songs WHERE id=?",
            (sid,),
        ).fetchone()
        assert saved["prep_state"] == "original"
        assert saved["skip_subtitles"] == 1
        assert (store.media / saved["media_file"]).read_bytes() == b"source-video"
        attempts = sorted((store.media / f"{sid}-auto-alignment").glob("attempt-*"))
        assert (
            json.loads((attempts[-1] / "attempt.json").read_text())["outcome"]
            == "stale"
        )


def test_real_stage_events_only_five_transitions(tmp_path, monkeypatch):
    from vc_ktv import autoprep

    app = make_app(tmp_path)
    store = app.state.store
    sid = add_song(store, "A", 1)
    stages = []

    monkeypatch.setenv("VC_KTV_PIPELINE_MODE", "quality")

    def fake_upstream(video, lyrics, dest, settings, *, style, progress, mode):
        assert mode == "quality"
        for message in (
            "读取原视频并提取音频",
            "Kim 分离用于推理的人声",
            "Kim 正在提取人声（已运行 30 秒）",
            "双遍 Qwen3-ASR 定位与 TIFA 安全分块对齐",
            "生成 ASS、LRC 与时间轴记录",
            "将字幕烧录到保留原始音轨的 MP4",
        ):
            progress(message)
            stages.append(
                store.db.execute(
                    "SELECT prep_stage FROM songs WHERE id=?", (sid,)
                ).fetchone()[0]
            )
        subtitles = dest / "subtitles"
        subtitles.mkdir(parents=True)
        ass = subtitles / "lyrics.ass"
        ass.write_text("ASS")
        (subtitles / "lyrics.lrc").write_text("[00:01.00]歌词")
        report = subtitles / "export_report.json"
        report.write_text('{"rendered_lines":1}')
        video = subtitles / "karaoke.mp4"
        video.write_bytes(b"mp4")
        return SimpleNamespace(video=video, ass=ass, report=report)

    monkeypatch.setattr(autoprep, "make_karaoke_video", fake_upstream)
    worker = AutoPrep(store, lambda path: 5)
    row = worker.claim()
    assert row["id"] == sid
    worker.prepare_subtitles(row)
    assert stages == ["extract", "kim", "kim", "align", "subtitles", "render"]
    recorded = store.db.execute(
        "SELECT prep_stage,prep_stage_at,prep_state FROM songs WHERE id=?",
        (sid,),
    ).fetchone()
    assert recorded["prep_stage"] == "done"
    assert recorded["prep_state"] == "done"
    assert recorded["prep_stage_at"] > 0


def test_cancel_manual_rendering_and_inflight_alignment(tmp_path):
    app = make_app(tmp_path)
    a = add_song(app.state.store, "A", 1, state="aligning")
    b = add_song(app.state.store, "B", 2, status="rendering")
    with TestClient(app) as guest, TestClient(app) as admin:
        assert guest.post(f"/api/admin/songs/{a}/cancel").status_code == 401
        login(admin)
        for song in (a, b):
            assert admin.post(f"/api/admin/songs/{song}/cancel").status_code == 200
            assert (
                app.state.store.db.execute(
                    "SELECT status FROM songs WHERE id=?", (song,)
                ).fetchone()[0]
                == "cancelled"
            )
            assert admin.post(f"/api/admin/songs/{song}/cancel").status_code == 409


def test_skip_settings_not_shared_between_events(tmp_path):
    app = make_app(tmp_path)
    sid = add_song(app.state.store, "A", 1)
    with TestClient(app) as admin:
        login(admin)
        assert admin.post(f"/api/admin/songs/{sid}/subtitles/skip").status_code == 200
        assert admin.post("/api/admin/events/close").status_code == 200
        assert (
            admin.post(
                "/api/admin/events", json={"name": "新活动", "code": "000002"}
            ).status_code
            == 200
        )
        other = add_song(app.state.store, "B", 2)
        state = admin.get("/api/admin/state").json()
        assert state["queue"] == []
        assert state["waiting"][0]["id"] == other
        assert state["waiting"][0]["skip_subtitles"] == 0
