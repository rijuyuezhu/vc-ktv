"""HTML-only preparation, safe publication, and conversion to a baked rendition."""

import json

from fastapi.testclient import TestClient

from tests.helpers import primary_tv_token
from tests.test_tv_html_source import room
from vc_ktv.core.artifacts import find_bundle
from vc_ktv.processing import media_preparation, refresh
from vc_ktv.processing.worker import AutoPrep

LRC = "[00:01.00]测试歌词\n[00:05.00]再次测试"
PROFILE = {
    "codec": "h264",
    "height": 720,
    "duration": 20,
    "average_mbps": 0.1,
    "peak_1s_mbps": 0.1,
    "peak_5s_mbps": 0.1,
}


def seed(app, *, source_size=2048):
    store = app.state.events.active()
    source = store.media / "original.mp4"
    source.write_bytes(b"S" * source_size)
    store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
    sid = store.db.execute(
        """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,
        status,prep_state,source_file,media_file,lyric_choice,lyric_timing_mode,provider_lrc,source_lrc_raw)
        VALUES('BV0000000001',1,'Song','UP','',20,'g',1,'ready','downloaded',?,?,
        '测试歌词 再次测试','provider',?,?)""",
        (source.name, source.name, LRC, LRC),
    ).lastrowid
    store.record_artifact(
        source.name,
        "source",
        {
            **PROFILE,
            "origin": "upload",
            "requested_codec": "h264",
            "bandwidth": 3,
        },
    )
    store.save_settings(None, None, original_codec_mode="h264")
    store.db.commit()
    return store, sid, source


def row(store, sid):
    return dict(store.db.execute("SELECT * FROM songs WHERE id=?", (sid,)).fetchone())


def no_render(*args, **kwargs):
    raise AssertionError("no subtitle burn expected")


def test_html_default_and_replay_then_bake_without_realigning(tmp_path, monkeypatch):
    app = room(tmp_path)
    store, sid, source = seed(app)
    worker = AutoPrep(store, lambda _: 20, gpu_alignment=False)
    monkeypatch.setattr(media_preparation, "render", no_render)
    claim = worker.claim("fast")
    assert claim["burn_subtitles"] is False
    worker.prepare_subtitles(claim)
    song = row(store, sid)
    assert song["prep_state"] == "done" and song["html_only"] == 1
    assert song["media_file"] == source.name
    assert len(list(store.media.glob("*.mp4"))) == 1
    bundle = find_bundle(store, song)
    assert bundle and json.loads((bundle / "timeline.json").read_text())["lines"]
    assert store.fair_queue()[0]["id"] == sid
    assert not store.downloadable(song)

    with TestClient(app) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        token = primary_tv_token(admin)
        headers = {"Authorization": f"Bearer {token}"}
        active = admin.post("/api/tv/next", headers=headers).json()["active"]
        assert active["id"] == sid and active["media_url"] is None
        assert (
            admin.get(active["html_overlay"]["source_url"]).content
            == source.read_bytes()
        )
        assert admin.get(active["html_overlay"]["timeline_url"]).status_code == 200
        assert admin.get(f"/api/songs/{sid}/download").status_code == 409
        assert admin.get(f"/api/songs/{sid}/download/source").status_code == 200
        assert (
            admin.post(
                "/api/tv/finish", headers=headers, json={"song_id": sid}
            ).status_code
            == 200
        )
        replay = admin.post(f"/api/admin/songs/{sid}/replay").json()["id"]
        assert row(store, replay)["html_only"] == 1
        assert [s["id"] for s in store.fair_queue()] == [replay]
        assert (
            admin.put("/api/admin/settings", json={"burn_subtitles": True}).status_code
            == 200
        )
        assert row(store, replay)["refresh_state"] == "queued"
        assert not store.fair_queue()
        # The already committed timing bundle is sufficient for a baked copy.
        monkeypatch.setattr(refresh, "known_media", lambda *a: PROFILE)
        monkeypatch.setattr(refresh, "inspect_media", lambda *a: PROFILE)
        monkeypatch.setattr(refresh, "render_provider_video", no_render)

        def bake(src, timing, target, limit):
            assert src == source and (timing / "timeline.json").is_file()
            target.write_bytes(b"baked")

        monkeypatch.setattr(refresh, "_render_from_bundle", bake)
        monkeypatch.setattr(refresh, "prepare_tv_source", lambda *a, **k: None)
        refresh.execute_refresh(worker, worker.claim("general"))
        new = row(store, replay)
        assert new["html_only"] == 0 and new["refresh_state"] == ""
        assert new["media_file"] != source.name
        assert (store.media / new["media_file"]).read_bytes() == b"baked"
        assert admin.get(f"/api/songs/{replay}/download").status_code == 200
        assert row(store, sid)["html_only"] == 1
        assert source.is_file() and find_bundle(store, row(store, sid))


def test_html_only_cache_failure_never_publishes_playable_song(tmp_path, monkeypatch):
    app = room(tmp_path)
    store, sid, source = seed(app, source_size=8_000_000)
    worker = AutoPrep(store, lambda _: 20, gpu_alignment=False)
    monkeypatch.setattr(media_preparation, "render", no_render)
    monkeypatch.setattr(
        "vc_ktv.processing.worker.prepare_tv_source", lambda *a, **k: None
    )
    worker.prepare_subtitles(worker.claim("fast"))
    assert row(store, sid)["prep_state"] == "original"
    assert source.is_file() and not store.fair_queue()


def test_html_only_mode_switch_queues_baked_copy(tmp_path, monkeypatch):
    app = room(tmp_path)
    store, sid, _ = seed(app)
    worker = AutoPrep(store, lambda _: 20, gpu_alignment=False)
    monkeypatch.setattr(media_preparation, "render", no_render)
    worker.prepare_subtitles(worker.claim("fast"))
    with TestClient(app) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "manual"}).status_code
            == 200
        )
        assert row(store, sid)["refresh_state"] == "queued"
        assert not store.fair_queue()
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        # Reverting the mode makes the previous HTML-only generation valid.
        assert [s["id"] for s in store.fair_queue()] == [sid]


def test_html_attempt_is_committed_not_inferred_from_filename(tmp_path, monkeypatch):
    import shutil

    app = room(tmp_path)
    store, sid, source = seed(app)
    worker = AutoPrep(store, lambda _: 20, gpu_alignment=False)
    monkeypatch.setattr(media_preparation, "render", no_render)
    worker.prepare_subtitles(worker.claim("fast"))
    song = row(store, sid)
    attempt = store.media / song["html_attempt"]
    other = attempt.parent / "attempt-99999999999999999999-stale"
    shutil.copytree(attempt, other)
    assert find_bundle(store, song).is_relative_to(attempt)
    # Loss of the current timing bundle cannot resurrect an older generation.
    (find_bundle(store, song) / "timeline.json").unlink()
    assert find_bundle(store, song) is None
    assert not store.fair_queue()
    assert "timeline" in store.refresh_reasons(song, store.settings())
    assert source.is_file()


def test_missing_required_cache_is_refreshable_without_realigning(
    tmp_path, monkeypatch
):
    app = room(tmp_path)
    store, sid, source = seed(app, source_size=8_000_000)
    worker = AutoPrep(store, lambda _: 20, gpu_alignment=False)
    monkeypatch.setattr(media_preparation, "render", no_render)

    def cache(src, archive, *args, **kwargs):
        (archive / "tv-source.mp4").write_bytes(b"C" * 2048)

    monkeypatch.setattr("vc_ktv.processing.worker.prepare_tv_source", cache)
    worker.prepare_subtitles(worker.claim("fast"))
    old = row(store, sid)
    assert old["html_only"] and store.fair_queue()
    old_archive = store.media / old["html_attempt"]
    (old_archive / "tv-source.mp4").unlink()
    assert not store.fair_queue()
    assert "tv_cache" in store.refresh_reasons(old, store.settings())
    with TestClient(app) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        details = admin.get(f"/api/songs/{sid}/detail")
        assert details.status_code == 200, details.text
        assert not details.json()["awaiting_backing"]
        assert admin.post(f"/api/admin/songs/{sid}/refresh").json()["count"] == 1
        monkeypatch.setattr(refresh, "known_media", lambda *a: PROFILE)
        monkeypatch.setattr(refresh, "prepare_tv_source", cache)
        monkeypatch.setattr(refresh, "render_provider_video", no_render)
        refresh.execute_refresh(worker, worker.claim("general"))
    new = row(store, sid)
    assert new["html_only"] and new["html_attempt"] != old["html_attempt"]
    assert new["media_file"] == source.name and store.fair_queue()
    assert (store.media / new["html_attempt"] / "tv-source.mp4").is_file()
    assert not store.refresh_reasons(new, store.settings())
