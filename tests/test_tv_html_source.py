"""Signed TV original video, audio-preserving cache and HTML plan selection."""

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.helpers import join_and_register, primary_tv_token
from vc_ktv.application.factory import create_app
from vc_ktv.processing.media_preparation import prepare_tv_source, stream_video_kbps


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
        assert initial["tv_html_overlay"] is True
        assert initial["max_bandwidth_mbps"] == 3.0
        assert "compact_video" not in initial
        for invalid in (0, 0.5, -1, "unlimited"):
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
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        token = primary_tv_token(admin)
        # Explicitly switching off still works; switching back on enables
        # the original video and HTML subtitles for the same song.
        disabled = admin.put(
            "/api/admin/settings",
            json={
                "queue_policy": "fair",
                "allow_unsubtitled": False,
                "tv_html_overlay": False,
            },
        )
        assert disabled.status_code == 200
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
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        token = primary_tv_token(admin)
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

    monkeypatch.setattr("vc_ktv.processing.media_preparation.subprocess.run", fake_run)
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


def test_virtual_replay_inherits_tv_subtitles_and_source_without_media_links(tmp_path):
    app = room(tmp_path)
    original_id, source, baked, _ = setup_song(app)
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "Singer")
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        token = primary_tv_token(admin)
        enable(admin)
        store = app.state.store
        with store.lock:
            store.db.execute(
                "UPDATE songs SET status='played' WHERE id=?", (original_id,)
            )
            store.db.commit()
        files_before = set(store.media.iterdir())
        first = guest.post(f"/api/songs/{original_id}/replay")
        assert first.status_code == 200, first.text
        first_id = first.json()["id"]
        assert set(store.media.iterdir()) == files_before
        with store.lock:
            record = store.db.execute(
                "SELECT media_file,source_file,replay_of FROM songs WHERE id=?",
                (first_id,),
            ).fetchone()
            assert tuple(record) == (None, None, original_id)
        state = guest.get("/api/state").json()
        assert next(s for s in state["queue"] if s["id"] == first_id)["downloadable"]
        assert guest.get(f"/api/songs/{first_id}/detail").json()["source_downloadable"]
        assert (
            guest.get(f"/api/songs/{first_id}/download").content == baked.read_bytes()
        )
        assert (
            guest.get(f"/api/songs/{first_id}/download/source").content
            == source.read_bytes()
        )

        first_active = guest.post("/api/tv/next", params={"token": token}).json()[
            "active"
        ]
        assert first_active["id"] == first_id
        assert first_active["html_overlay"] is not None
        metadata = first_active["html_overlay"]
        assert guest.get(metadata["source_url"]).content == source.read_bytes(), (
            guest.get(metadata["source_url"]).text
        )
        assert (
            guest.get(metadata["timeline_url"]).json()["timeline"]["lines"][0]["text"]
            == "歌词"
        )
        assert guest.get(first_active["media_url"]).content == baked.read_bytes()
        assert (
            guest.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": first_id}
            ).status_code
            == 200
        )

        second = guest.post(f"/api/songs/{first_id}/replay")
        assert second.status_code == 200, second.text
        second_id = second.json()["id"]
        assert set(store.media.iterdir()) == files_before
        with store.lock:
            second_record = store.db.execute(
                "SELECT media_file,source_file,replay_of FROM songs WHERE id=?",
                (second_id,),
            ).fetchone()
            assert tuple(second_record) == (None, None, first_id)
        second_active = guest.post("/api/tv/next", params={"token": token}).json()[
            "active"
        ]
        assert second_active["id"] == second_id
        assert second_active["html_overlay"] is not None
        assert (
            guest.get(second_active["html_overlay"]["source_url"]).content
            == source.read_bytes()
        )
        assert (
            guest.get(second_active["html_overlay"]["timeline_url"]).status_code == 200
        )
        assert (
            guest.get(f"/api/songs/{second_id}/download").content == baked.read_bytes()
        )
        # A child can own a different rendered video, without mutating the
        # parent or accidentally reusing the parent's HTML subtitle plan.
        custom = store.media / "custom.mp4"
        custom.write_bytes(b"CUSTOM")
        with store.lock:
            store.db.execute(
                "UPDATE songs SET media_file=? WHERE id=?", (custom.name, second_id)
            )
            store.db.commit()
        second_state = guest.get("/api/tv/state", params={"token": token}).json()[
            "active"
        ]
        assert second_state["html_overlay"] is None
        assert guest.get(second_state["media_url"]).content == b"CUSTOM"
        assert (
            guest.get(f"/api/songs/{first_id}/download").content == baked.read_bytes()
        )


def test_legacy_hardlink_replay_inherits_overlay_only_when_media_match(tmp_path):
    import os

    app = room(tmp_path)
    original_id, source, baked, _ = setup_song(app)
    with TestClient(app) as admin, TestClient(app) as guest:
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        token = primary_tv_token(admin)
        enable(admin)
        store = app.state.store
        old_source = store.media / "legacy-replay-original.mp4"
        old_baked = store.media / "legacy-replay-baked.mp4"
        os.link(source, old_source)
        os.link(baked, old_baked)
        with store.lock:
            parent = store.db.execute(
                "SELECT * FROM songs WHERE id=?", (original_id,)
            ).fetchone()
            store.db.execute(
                "UPDATE songs SET status='played' WHERE id=?", (original_id,)
            )
            replay_id = store.db.execute(
                """INSERT INTO songs(
                    bvid,page,title,uploader,cover,duration,requester,requested_at,
                    status,prep_state,lyric_choice,lyrics,media_file,source_file,replay_of)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    parent["bvid"],
                    parent["page"],
                    parent["title"],
                    parent["uploader"],
                    parent["cover"],
                    parent["duration"],
                    parent["requester"],
                    2,
                    "ready",
                    "done",
                    parent["lyric_choice"],
                    parent["lyrics"],
                    old_baked.name,
                    old_source.name,
                    original_id,
                ),
            ).lastrowid
            store.db.commit()
        old = guest.post("/api/tv/next", params={"token": token}).json()["active"]
        assert old["id"] == replay_id
        assert old["html_overlay"] is not None
        assert guest.get(old["html_overlay"]["timeline_url"]).status_code == 200
        assert (
            guest.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": replay_id}
            ).status_code
            == 200
        )
        # Independently edited legacy media must not borrow a mismatched plan.
        replacement = store.media / "different.mp4"
        replacement.write_bytes(b"DIFFERENT")
        with store.lock:
            store.db.execute(
                "UPDATE songs SET media_file=?,status='ready' WHERE id=?",
                (replacement.name, replay_id),
            )
            store.db.commit()
        updated = guest.post("/api/tv/next", params={"token": token}).json()["active"]
        assert updated["id"] == replay_id
        assert updated["html_overlay"] is None


def test_virtual_replay_retains_source_bitrate_cache_and_isolates_upload(tmp_path):
    app = room(tmp_path)
    original_id, original, baked, archive = setup_song(
        app, source_size=4_000_000, duration=2
    )
    with TestClient(app) as admin, TestClient(app) as guest:
        join_and_register(guest, "Singer")
        assert (
            admin.post("/api/admin/login", json={"password": "pw"}).status_code == 200
        )
        assert (
            admin.put("/api/admin/playback-mode", json={"mode": "tv"}).status_code
            == 200
        )
        token = primary_tv_token(admin)
        enable(admin)
        store = app.state.store
        with store.lock:
            store.db.execute(
                "UPDATE songs SET status='played' WHERE id=?", (original_id,)
            )
            store.db.commit()
        replay = guest.post(f"/api/songs/{original_id}/replay")
        assert replay.status_code == 200
        replay_id = replay.json()["id"]
        active = guest.post("/api/tv/next", params={"token": token}).json()["active"]
        assert active["id"] == replay_id
        assert active["html_overlay"] is None  # Fast fallback, without cache.
        cache = archive / "tv-source.mp4"
        cache.write_bytes(b"COMPRESSED")
        active = guest.get("/api/tv/state", params={"token": token}).json()["active"]
        assert active["html_overlay"] is not None
        assert guest.get(active["html_overlay"]["source_url"]).content == b"COMPRESSED"
        assert guest.get(active["html_overlay"]["timeline_url"]).status_code == 200
        # A different bitrate setting must invalidate inherited cache.
        assert (
            admin.put(
                "/api/admin/settings",
                json={
                    "queue_policy": "fair",
                    "allow_unsubtitled": False,
                    "max_bandwidth_mbps": 2.0,
                },
            ).status_code
            == 200
        )
        assert (
            guest.get("/api/tv/state", params={"token": token}).json()["active"][
                "html_overlay"
            ]
            is None
        )
        assert guest.get(active["html_overlay"]["source_url"]).status_code == 409
        assert (
            guest.post(
                "/api/tv/finish", params={"token": token}, json={"song_id": replay_id}
            ).status_code
            == 200
        )
        with store.lock:
            store.db.execute("UPDATE songs SET status='ready' WHERE id=?", (replay_id,))
            store.db.commit()
        app.state.probe = lambda path: 2
        uploaded = admin.post(
            f"/api/admin/songs/{replay_id}/media",
            files={"file": ("custom.mp4", b"UPLOADED", "video/mp4")},
        )
        assert uploaded.status_code == 200, uploaded.text
        with store.lock:
            row = store.db.execute(
                "SELECT media_file,source_file,replay_of FROM songs WHERE id=?",
                (replay_id,),
            ).fetchone()
            assert row["media_file"] == row["source_file"]
            assert row["replay_of"] == original_id
        assert guest.get(f"/api/songs/{replay_id}/download").content == b"UPLOADED"
        assert (
            guest.get(f"/api/songs/{original_id}/download").content
            == baked.read_bytes()
        )
        assert original.is_file()
        assert (
            guest.post("/api/tv/next", params={"token": token}).json()["active"][
                "html_overlay"
            ]
            is None
        )
