"""Admin upload and TextGrid changes do not publish stale files or lyrics."""

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song, new_app
from tests.test_textgrid_import import grid_file
from vc_ktv.application import admin_preparation


def login(client):
    response = client.post("/api/admin/login", json={"password": "test-admin"})
    assert response.status_code == 200


@pytest.mark.parametrize("new_status", ["playing", "played", "cancelled"])
def test_upload_discards_media_if_song_changes_during_probe(tmp_path, new_status):
    app = new_app(tmp_path)
    store = app.state.store
    song_id = add_song(store, "singer", media="old.mp4", prep="manual")
    old = store.media / "old.mp4"

    def probe(_path):
        with store.lock:
            store.db.execute(
                "UPDATE songs SET status=? WHERE id=?", (new_status, song_id)
            )
            store.db.commit()
        return 12

    app.state.probe = probe
    with TestClient(app) as admin:
        login(admin)
        response = admin.post(
            f"/api/admin/songs/{song_id}/media",
            files={"file": ("replacement.mp4", b"replacement video", "video/mp4")},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "歌曲状态已变化"
    row = store.rows(
        "SELECT status,media_file,source_file FROM songs WHERE id=?", (song_id,)
    )[0]
    assert row["status"] == new_status
    assert row["media_file"] == old.name
    assert row["source_file"] == old.name
    assert old.read_bytes() == b"valid-pretend-mp4"
    assert {path.name for path in store.media.iterdir()} == {old.name}
    assert not store.db.in_transaction


@pytest.mark.parametrize("mutation", ["lyrics", "playing"])
def test_textgrid_import_discards_archive_when_song_changes_during_conversion(
    tmp_path, monkeypatch, mutation
):
    app = new_app(tmp_path)
    store = app.state.store
    song_id = add_song(store, "singer", media="source.mp4", prep="manual")
    original_convert = admin_preparation.convert_textgrid

    def changed_during_conversion(*args, **kwargs):
        result = original_convert(*args, **kwargs)
        with store.lock:
            if mutation == "lyrics":
                store.db.execute(
                    "UPDATE songs SET lyrics='updated by editor' WHERE id=?", (song_id,)
                )
            else:
                store.db.execute(
                    "UPDATE songs SET status='playing' WHERE id=?", (song_id,)
                )
            store.db.commit()
        return result

    monkeypatch.setattr(
        admin_preparation, "convert_textgrid", changed_during_conversion
    )
    grid = grid_file(tmp_path / "timeline.TextGrid").read_bytes()
    with TestClient(app) as admin:
        login(admin)
        assert (
            admin.post(
                f"/api/admin/songs/{song_id}/lyrics", json={"text": "你，好!\n下一句"}
            ).status_code
            == 200
        )
        response = admin.post(
            f"/api/admin/songs/{song_id}/alignment/import",
            files={"file": ("timeline.TextGrid", grid, "text/plain")},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "歌词或歌曲状态发生变化，请重新导入"
    row = store.rows("SELECT status,lyrics FROM songs WHERE id=?", (song_id,))[0]
    assert row["status"] == ("playing" if mutation == "playing" else "ready")
    assert row["lyrics"] == (
        "updated by editor" if mutation == "lyrics" else "你，好!\n下一句"
    )
    assert not list(store.media.glob(f"{song_id}-alignment-*"))
    assert not store.db.in_transaction
