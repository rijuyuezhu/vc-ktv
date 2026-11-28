"""Signed accompaniment assets must follow the current event and replay source."""

from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app
from vc_ktv.processing.backing_audio import KARAOKE_SHA256, source_identity


@pytest.fixture(autouse=True)
def enable_backing(monkeypatch):
    monkeypatch.setenv("VC_KTV_BACKING_TRACKS", "1")


def prepared_room(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test",
        secret=b"backing-player-test-key",
        auto_prepare=False,
    )
    app.state.events.create("test event", "123456")
    store = app.state.events.active()
    source = store.media / "1-original.mp4"
    source.write_bytes(b"video original" * 200)
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Guest')")
        store.db.execute(
            """INSERT INTO songs
               (id,bvid,page,title,uploader,cover,duration,requester,requested_at,
                status,prep_state,media_file,source_file)
               VALUES(1,'BV0000000001',1,'Song','UP','',25,'g',1,'playing',
                      'original',?,?)""",
            (source.name, source.name),
        )
        store.db.commit()
    key = source_identity(source, KARAOKE_SHA256)
    backing = store.root / "backing" / f"{key}.m4a"
    backing.parent.mkdir()
    return app, store, source, key, backing


def ready(store, source, key, backing):
    backing.write_bytes(b"generated AAC data" * 100)
    with store.lock:
        store.db.execute(
            """INSERT INTO backing_tracks
               (cache_key,source_file,model_sha256,status)
               VALUES(?,?,?,'ready')""",
            (key, source.name, KARAOKE_SHA256),
        )
        store.db.commit()


def test_signed_audio_stream_and_no_token(tmp_path):
    app, store, source, key, backing = prepared_room(tmp_path)
    with TestClient(app) as client:
        state = app.state.media.active_view()
        assert state["backing"] == {"status": "pending"}
        assert client.get("/media/1/backing").status_code == 403
        ready(store, source, key, backing)
        state = app.state.media.active_view()
        assert state["backing"]["status"] == "ready"
        assert state["backing"]["cache_key"] == key
        url = state["backing"]["url"]
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.content == backing.read_bytes()
        assert response.headers["content-type"] == "audio/mp4"
        assert client.get(urlsplit(url).path).status_code == 403
        assert client.get(url.replace("token=", "token=bad")).status_code == 403
        # A video authorization token is not an accompaniment token.
        token = app.state.media.signed_media(state).partition("token=")[2]
        assert client.get(f"/media/1/backing?token={token}").status_code == 403


def test_replay_reuses_audio_with_song_scoped_signature(tmp_path):
    app, store, source, key, backing = prepared_room(tmp_path)
    ready(store, source, key, backing)
    with store.lock:
        store.db.execute("UPDATE songs SET status='played' WHERE id=1")
        store.db.execute(
            """INSERT INTO songs
               (id,bvid,page,title,uploader,cover,duration,requester,requested_at,
                status,prep_state,replay_of)
               VALUES(2,'BV0000000001',1,'Song','UP','',25,'g',2,'playing',
                      'original',1)"""
        )
        store.db.commit()
    with TestClient(app) as client:
        state = app.state.media.active_view()
        assert state["id"] == 2
        url = state["backing"]["url"]
        assert state["backing"]["status"] == "ready"
        assert client.get(url).content == backing.read_bytes()
        copied_token = url.partition("token=")[2]
        assert client.get(f"/media/1/backing?token={copied_token}").status_code == 403


def test_changed_source_and_missing_asset_cannot_serve_stale_audio(tmp_path):
    app, store, source, key, backing = prepared_room(tmp_path)
    ready(store, source, key, backing)
    with TestClient(app) as client:
        old_url = app.state.media.active_view()["backing"]["url"]
        backing.unlink()
        assert app.state.media.active_view()["backing"]["status"] == "pending"
        assert client.get(old_url).status_code == 404
        backing.write_bytes(b"generated AAC data" * 100)
        source.write_bytes(b"replacement original" * 200)
        assert app.state.media.active_view()["backing"]["status"] == "pending"
        assert client.get(old_url).status_code == 404


def test_failed_or_processing_assets_are_never_downloadable(tmp_path):
    app, store, source, key, backing = prepared_room(tmp_path)
    ready(store, source, key, backing)
    with TestClient(app) as client:
        signed_url = app.state.media.active_view()["backing"]["url"]
        for status in ("processing", "failed", "pending"):
            with store.lock:
                store.db.execute(
                    "UPDATE backing_tracks SET status=? WHERE cache_key=?",
                    (status, key),
                )
                store.db.commit()
            assert app.state.media.active_view()["backing"]["status"] == status
            assert client.get(signed_url).status_code == 404


def test_refuse_backing_storage_symlink(tmp_path):
    app, store, source, key, backing = prepared_room(tmp_path)
    ready(store, source, key, backing)
    with TestClient(app) as client:
        signed_url = app.state.media.active_view()["backing"]["url"]
        backing.unlink()
        external = tmp_path / "external.m4a"
        external.write_bytes(b"private bytes" * 200)
        backing.symlink_to(external)
        assert app.state.media.active_view()["backing"]["status"] == "pending"
        assert client.get(signed_url).status_code == 404
        assert external.read_bytes() == b"private bytes" * 200


def test_backing_token_is_invalid_after_event_transition(tmp_path):
    app, store, source, key, backing = prepared_room(tmp_path)
    ready(store, source, key, backing)
    with TestClient(app) as client:
        old_url = app.state.media.active_view()["backing"]["url"]
        app.state.events.close()
        assert client.get(old_url).status_code == 403


def test_report_accepts_audio_mode_and_legacy_report_defaults():
    import pytest
    from pydantic import ValidationError

    from vc_ktv.application.schemas import TVRemoteReport

    assert TVRemoteReport(ack_seq=0).audio_mode == "original"
    assert TVRemoteReport(ack_seq=2, audio_mode="backing").audio_mode == "backing"
    with pytest.raises(ValidationError):
        TVRemoteReport(ack_seq=1, audio_mode="unknown")


def test_disabled_backing_hides_cached_audio_and_gate(tmp_path, monkeypatch):
    app, store, source, key, backing = prepared_room(tmp_path)
    ready(store, source, key, backing)
    with TestClient(app) as client:
        url = app.state.media.active_view()["backing"]["url"]
        store.save_settings(None, None, require_backing_ready=True)
        assert store.settings()["require_backing_ready"]
        monkeypatch.setenv("VC_KTV_BACKING_TRACKS", "0")
        assert app.state.media.active_view()["backing"] == {"status": "unavailable"}
        assert client.get(url).status_code == 404
        assert not store.settings()["require_backing_ready"]
        assert backing.is_file()
