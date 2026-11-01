"""Signed TV original video, audio-preserving cache and HTML plan selection."""

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.autoprep import prepare_tv_source, stream_video_kbps


def room(tmp_path):
    app = create_app(
        root=tmp_path / "ktv",
        password="pw",
        secret=b"tv-overlay-test",
        auto_prepare=False,
    )
    app.state.events.create("TV Test", "123456")
    return app


def setup_song(app, *, source_size=100, duration=20):
    store = app.state.store
    original = store.media / "original.mp4"
    original.write_bytes(b"O" * source_size)
    baked = store.media / "baked.mp4"
    baked.write_bytes(b"BAKED")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES ('guest','Guest')")
        song_id = store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,"
            "status,prep_state,media_file,source_file,lyric_choice) "
            "VALUES ('BV0000000001',1,'Song','Author','',?,'guest',1,'ready','done',?,?,?)",
            (duration, baked.name, original.name, "two complete lyric lines"),
        ).lastrowid
        store.db.commit()
    archive = store.media / f"{song_id}-auto-alignment" / "attempt-10-aa"
    bundle = archive / "pipeline" / "subtitles"
    bundle.mkdir(parents=True)
    (bundle / "timeline.json").write_text(
        json.dumps({"lines": [{"id": "x", "text": "歌词"}]})
    )
    (bundle / "display_plan.json").write_text(
        json.dumps({"x": {"start": 1, "end": 5, "slot": 0}})
    )
    (bundle / "style.json").write_text(
        json.dumps({"mode": "double", "size": 48, "position": 0.86})
    )
    (archive / "attempt.json").write_text(
        json.dumps(
            {
                "outcome": "rendered",
                "source_file": original.name,
                "max_bandwidth_mbps": 3.0,
            }
        )
    )
    return song_id, original, baked, archive


def enable(owner):
    response = owner.put(
        "/api/admin/settings",
        json={
            "queue_policy": "fair",
            "allow_unsubtitled": False,
            "tv_html_overlay": True,
            "max_bandwidth_mbps": 3.0,
        },
    )
    assert response.status_code == 200, response.text


def test_admin_controls_source_mode_and_bandwidth_per_event(tmp_path):
    app = room(tmp_path)
    with TestClient(app) as admin, TestClient(app) as other:
        assert (
            other.put(
                "/api/admin/settings",
                json={
                    "queue_policy": "fair",
                    "allow_unsubtitled": False,
                    "tv_html_overlay": True,
                },
            ).status_code
            == 401
        )
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        initial = admin.get("/api/admin/settings").json()
        assert initial["tv_html_overlay"] is False
        assert initial["max_bandwidth_mbps"] == 3.0
        assert "compact_video" not in initial
        for invalid in (0, 0.5, 12.5, -1, "unlimited"):
            resp = admin.put(
                "/api/admin/settings",
                json={
                    "queue_policy": "fair",
                    "allow_unsubtitled": False,
                    "max_bandwidth_mbps": invalid,
                },
            )
            assert resp.status_code == 422, (invalid, resp.text)
        enable(admin)
        assert admin.get("/api/admin/settings").json()["tv_html_overlay"] is True
        assert (
            admin.put(
                "/api/admin/settings",
                json={"queue_policy": "fair", "allow_unsubtitled": False},
            ).status_code
            == 200
        )
        assert admin.get("/api/admin/settings").json()["max_bandwidth_mbps"] == 3.0
        assert stream_video_kbps(3.0) == 2616
        assert stream_video_kbps(4.5) == 4116


def test_signed_original_and_timeline_preserve_baked_download(tmp_path):
    app = room(tmp_path)
    song_id, original, baked, archive = setup_song(app)
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "Singer")
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        token = admin.get("/api/admin/tv-link").json()["url"].partition("token=")[2]
        active = guest.post("/api/tv/next", params={"token": token}).json()["active"]
        assert active["html_overlay"] is None
        enable(admin)
        current = guest.get("/api/tv/state", params={"token": token}).json()["active"]
        metadata = current["html_overlay"]
        assert metadata is not None
        assert guest.get(metadata["source_url"]).content == original.read_bytes()
        subtitle = guest.get(metadata["timeline_url"]).json()
        assert subtitle["timeline"]["lines"][0]["text"] == "歌词"
        assert subtitle["plan"]["x"]["slot"] == 0
        assert guest.get("/media/1/source", params={"token": "bad"}).status_code == 403
        assert (
            guest.get("/api/tv/subtitles/1", params={"token": "bad"}).status_code == 403
        )
        assert guest.get(active["media_url"]).content == baked.read_bytes()
        assert guest.get(f"/api/songs/{song_id}/download").content == baked.read_bytes()


def test_large_original_requires_matching_bitrate_cache(tmp_path):
    app = room(tmp_path)
    song_id, original, baked, archive = setup_song(
        app, source_size=4_000_000, duration=2
    )
    with TestClient(app) as admin, TestClient(app) as guest:
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        token = admin.get("/api/admin/tv-link").json()["url"].partition("token=")[2]
        enable(admin)
        first = guest.post("/api/tv/next", params={"token": token}).json()["active"]
        assert first["html_overlay"] is None
        (archive / "tv-source.mp4").write_bytes(b"COMPRESSED")
        active = guest.get("/api/tv/state", params={"token": token}).json()["active"]
        assert active["html_overlay"] is not None
        assert guest.get(active["html_overlay"]["source_url"]).content == b"COMPRESSED"
        res = admin.put(
            "/api/admin/settings",
            json={
                "queue_policy": "fair",
                "allow_unsubtitled": False,
                "max_bandwidth_mbps": 2.0,
            },
        )
        assert res.status_code == 200
        assert (
            guest.get("/api/tv/state", params={"token": token}).json()["active"][
                "html_overlay"
            ]
            is None
        )
        assert guest.get(active["html_overlay"]["source_url"]).status_code == 409
        assert guest.get(first["media_url"]).content == baked.read_bytes()


def test_compress_source_only_if_average_too_large_and_keep_original(
    monkeypatch, tmp_path
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"ORIGINAL" * 5000)
    target = tmp_path / "archive"
    target.mkdir()
    requests = []

    def fake_run(cmd, **kwargs):
        requests.append(cmd)
        Path(cmd[-1]).write_bytes(b"X" * 2048)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("vc_ktv.autoprep.subprocess.run", fake_run)
    prepare_tv_source(source, target, 20, 3.0)
    assert requests == []
    prepare_tv_source(source, target, 0.02, 3.0)
    assert requests and "-maxrate" in requests[0]
    assert requests[0][requests[0].index("-maxrate") + 1] == "2616k"
    assert (
        "-c:a" in requests[0] and requests[0][requests[0].index("-c:a") + 1] == "copy"
    )
    assert (target / "tv-source.mp4").read_bytes() == b"X" * 2048
    assert source.read_bytes() == b"ORIGINAL" * 5000
