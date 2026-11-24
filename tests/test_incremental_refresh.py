"""Refresh only stale artifacts, preserving order and reusable media."""

import json
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from tests.test_tv_html_source import room, setup_song
from vc_ktv.core.artifacts import find_bundle, source_record
from vc_ktv.processing import refresh
from vc_ktv.processing.media_preparation import render_provider_video
from vc_ktv.processing.media_properties import inspect_media
from vc_ktv.processing.task_state import record_failure
from vc_ktv.processing.worker import AutoPrep


def login(client):
    assert client.post("/api/admin/login", json={"password": "pw"}).status_code == 200


def get_song(store, song_id):
    return dict(
        store.db.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
    )


def seed_source(store, source, *, codec="h264", height=720, ceiling=720):
    settings = {**store.settings(), "video_quality": ceiling}
    store.record_artifact(
        source.name,
        "source",
        source_record(
            {},
            settings,
            {"codec": codec, "height": height},
            origin="bilibili",
            lineage=source.name,
        ),
    )
    store.db.commit()


def test_invalidation_batch_revert_and_current_playback(tmp_path):
    app = room(tmp_path)
    store = app.state.store
    store.save_settings(None, None, video_quality=720, original_codec_mode="h264")
    song_id, source, _, _ = setup_song(app)
    seed_source(store, source)
    with TestClient(app) as owner, TestClient(app) as guest:
        login(owner)
        assert (
            owner.put(
                "/api/admin/settings",
                json={"original_codec_mode": "av1", "allow_unsubtitled": True},
            ).json()["outdated_count"]
            == 1
        )
        assert not store.fair_queue()
        assert store.refresh_reasons(get_song(store, song_id)) == ["codec"]
        assert guest.post("/api/admin/songs/refresh-outdated").status_code == 401
        assert owner.post("/api/admin/songs/refresh-outdated").json()["count"] == 1
        assert owner.post(f"/api/admin/songs/{song_id}/refresh").json()["count"] == 0
        assert (
            owner.put(
                "/api/admin/settings", json={"original_codec_mode": "h264"}
            ).json()["outdated_count"]
            == 0
        )
        with store.lock:
            store.claim_next()
        assert get_song(store, song_id)["status"] == "playing"
        assert (
            owner.put(
                "/api/admin/settings", json={"original_codec_mode": "av1"}
            ).json()["outdated_count"]
            == 0
        )
        assert owner.post("/api/admin/songs/refresh-outdated").json()["count"] == 0
        assert get_song(store, song_id)["source_file"] == source.name


def test_resolution_fallback_and_measured_bandwidth(tmp_path):
    app = room(tmp_path)
    store = app.state.store
    store.save_settings(None, None, video_quality=720, original_codec_mode="h264")
    song_id, source, baked, _ = setup_song(app)
    seed_source(store, source, height=480, ceiling=720)
    store.record_artifact(
        baked.name,
        "render",
        {
            "subtitle_style": "classic",
            "bandwidth": 3,
            "average_mbps": 0.6,
            "peak_1s_mbps": 0.9,
            "peak_5s_mbps": 0.8,
        },
    )
    store.db.commit()
    with TestClient(app) as owner:
        login(owner)
        result = owner.put(
            "/api/admin/settings", json={"video_quality": 720, "max_bandwidth_mbps": 1}
        )
        assert result.json()["outdated_count"] == 0
        result = owner.put("/api/admin/settings", json={"video_quality": 1080})
        assert result.json()["outdated_count"] == 1
        assert store.refresh_reasons(get_song(store, song_id)) == ["quality"]


def real_prepared(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    app = room(tmp_path)
    store = app.state.store
    store.save_settings(None, None, original_codec_mode="h264")
    song_id, source, baked, archive = setup_song(app, duration=3)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    shutil.rmtree(archive)
    render_provider_video(
        source, "[00:00.50]测试歌词\n[00:01.50]再次测试", 0, baked, archive, 3
    )
    (archive / "attempt.json").write_text(
        json.dumps(
            {
                "outcome": "rendered",
                "source_file": source.name,
                "media_file": baked.name,
                "subtitle_style": "classic",
                "max_bandwidth_mbps": 3,
            }
        )
    )
    store.record_artifact(
        source.name,
        "source",
        source_record(
            {},
            store.settings(),
            inspect_media(source),
            origin="upload",
            lineage=source.name,
        ),
    )
    store.record_artifact(
        baked.name,
        "render",
        {**inspect_media(baked), "subtitle_style": "classic", "bandwidth": 3},
    )
    store.db.execute(
        "UPDATE songs SET lyrics=?, lyric_timing_mode='provider',provider_lrc=? WHERE id=?",
        ("测试歌词", "[00:00.50]测试歌词\n[00:01.50]再次测试", song_id),
    )
    store.db.commit()
    return app, song_id, source, baked, archive


def test_style_only_uses_existing_timeline_and_original(tmp_path, monkeypatch):
    app, song_id, source, baked, archive = real_prepared(tmp_path)
    store = app.state.store
    previous = get_song(store, song_id)
    timeline = json.loads(
        (archive / "provider_subtitles" / "timeline.json").read_text()
    )

    def forbid(*args, **kwargs):
        raise AssertionError("style refresh must not download or align")

    worker = AutoPrep(store, lambda _: 3, download=forbid, enhance=forbid)
    monkeypatch.setattr(refresh, "render_provider_video", forbid)
    with TestClient(app) as admin:
        login(admin)
        assert (
            admin.put("/api/admin/settings", json={"subtitle_style": "large"}).json()[
                "outdated_count"
            ]
            == 1
        )
        assert admin.post(f"/api/admin/songs/{song_id}/refresh").json()["count"] == 1
        claimed = worker.claim()
        assert claimed["refresh_task"]
        refresh.execute_refresh(worker, claimed)
        updated = get_song(store, song_id)
        assert updated["source_file"] == source.name
        assert updated["media_file"] != baked.name
        assert (store.media / updated["media_file"]).is_file()
        assert updated["requested_at"] == previous["requested_at"]
        assert updated["refresh_state"] == ""
        assert not store.refresh_reasons(updated)
        bundle = find_bundle(store, updated)
        assert bundle is not None
        assert json.loads((bundle / "timeline.json").read_text()) == timeline
        assert json.loads((bundle / "style.json").read_text())["size"] == 62
        assert baked.is_file()
        assert store.fair_queue()[0]["id"] == song_id


def test_failed_refresh_preserves_old_generation(tmp_path, monkeypatch):
    app, song_id, source, baked, _ = real_prepared(tmp_path)
    store = app.state.store
    worker = AutoPrep(store, lambda _: 3)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(refresh, "_render_from_bundle", fail)
    with TestClient(app) as admin:
        login(admin)
        admin.put("/api/admin/settings", json={"subtitle_style": "large"})
        admin.post(f"/api/admin/songs/{song_id}/refresh")
        claimed = worker.claim()
        with pytest.raises(RuntimeError) as error:
            refresh.execute_refresh(worker, claimed)
        record_failure(store, claimed, error.value, time.monotonic())
        updated = get_song(store, song_id)
        assert updated["refresh_state"] == "failed"
        assert (
            updated["source_file"] == source.name
            and updated["media_file"] == baked.name
        )
        assert not store.fair_queue()
        assert admin.post(f"/api/admin/songs/{song_id}/refresh").json()["count"] == 1


def test_native_codec_variant_reuses_timeline_and_backing(tmp_path, monkeypatch):
    from vc_ktv.processing.backing_audio import source_identity

    app, song_id, source, baked, archive = real_prepared(tmp_path)
    store = app.state.store
    native = tmp_path / "native.mp4"
    shutil.copyfile(source, native)
    # Represent the existing AV1 source as a distinct, actual AV1 video with
    # identical audio packets. The provider offers the original H.264 stream.
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(native),
            "-c:v",
            "libaom-av1",
            "-cpu-used",
            "8",
            "-threads",
            "2",
            "-c:a",
            "copy",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    assert inspect_media(source)["codec"] == "av1"
    store.save_settings(None, None, video_quality=720, original_codec_mode="av1")
    store.record_artifact(
        source.name,
        "source",
        source_record(
            {},
            store.settings(),
            inspect_media(source),
            origin="bilibili",
            lineage=source.name,
        ),
    )
    store.db.commit()
    observed = []

    def download(bvid, page, folder, **kwargs):
        observed.append(kwargs)
        target = folder / "native.mp4"
        shutil.copyfile(native, target)
        return target

    def forbid(*args, **kwargs):
        raise AssertionError("do not realign audio")

    # Production download uses codec preference keywords.
    monkeypatch.setattr(refresh, "media_download", download)
    worker = AutoPrep(store, lambda _: 3, download=download, enhance=forbid)
    monkeypatch.setattr(refresh, "render_provider_video", forbid)
    key = source_identity(source, worker.backing.model_sha256)
    backing = store.root / "backing"
    backing.mkdir(exist_ok=True)
    old_audio = backing / (key + ".m4a")
    old_audio.write_bytes(b"audio" * 400)
    store.db.execute(
        "INSERT INTO backing_tracks(cache_key,source_file,model_sha256,status,updated_at) VALUES(?,?,?,'ready',0)",
        (key, source.name, worker.backing.model_sha256),
    )
    store.db.commit()
    with TestClient(app) as admin:
        login(admin)
        assert (
            admin.put(
                "/api/admin/settings", json={"original_codec_mode": "h264"}
            ).json()["outdated_count"]
            == 1
        )
        admin.post(f"/api/admin/songs/{song_id}/refresh")
        claimed = worker.claim()
        refresh.execute_refresh(worker, claimed)
        updated = get_song(store, song_id)
        assert observed == [{"max_height": 720, "prefer_h264": True}]
        assert inspect_media(store.media / updated["source_file"])["codec"] == "h264"
        assert updated["source_file"] != source.name
        assert store.backing_ready(updated)
        assert store.refresh_reasons(updated) == []
        assert (find_bundle(store, updated) / "timeline.json").read_bytes() == (
            archive / "provider_subtitles" / "timeline.json"
        ).read_bytes()
        assert baked.exists()


def test_change_during_refresh_never_commits_stale_generation(tmp_path, monkeypatch):
    app, song_id, source, baked, _ = real_prepared(tmp_path)
    store = app.state.store
    worker = AutoPrep(store, lambda _: 3)
    original_render = refresh._render_from_bundle

    def changed(*args, **kwargs):
        original_render(*args, **kwargs)
        store.save_settings(None, None, subtitle_style="compact")

    monkeypatch.setattr(refresh, "_render_from_bundle", changed)
    with TestClient(app) as admin:
        login(admin)
        admin.put("/api/admin/settings", json={"subtitle_style": "large"})
        admin.post(f"/api/admin/songs/{song_id}/refresh")
        refresh.execute_refresh(worker, worker.claim())
        updated = get_song(store, song_id)
        assert updated["source_file"] == source.name
        assert updated["media_file"] == baked.name
        assert updated["refresh_state"] == "queued"
        assert not store.fair_queue()
