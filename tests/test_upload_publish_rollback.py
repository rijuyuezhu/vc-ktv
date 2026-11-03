"""An unsuccessful admin upload must not publish or retain a new video."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import add_song, new_app


@pytest.mark.parametrize("fail_at", ("update", "commit"))
@pytest.mark.parametrize("initial_status", ("ready", "waiting"))
def test_upload_publication_failure_preserves_previous_media(
    tmp_path, fail_at, initial_status
):
    app = new_app(tmp_path)
    app.state.probe = lambda path: 60
    store = app.state.events.current_store
    song_id = add_song(
        store,
        "singer",
        status=initial_status,
        prep="manual" if initial_status == "ready" else "pending",
        media="previous.mp4" if initial_status == "ready" else None,
    )
    original = store.media / "previous.mp4"
    if initial_status == "waiting":
        original.write_bytes(b"previous-source")
        with store.lock:
            store.db.execute(
                "UPDATE songs SET source_file=? WHERE id=?", (original.name, song_id)
            )
            store.db.commit()

    underlying = store.db

    class FailPublication:
        publishing = False
        did_fail = False

        def execute(self, sql, args=()):
            if sql.startswith("UPDATE songs SET status='ready',media_file="):
                if fail_at == "update":
                    self.did_fail = True
                    raise sqlite3.OperationalError(
                        "simulated admin upload UPDATE error"
                    )
                self.publishing = True
            return underlying.execute(sql, args)

        def commit(self):
            if self.publishing and not self.did_fail:
                self.did_fail = True
                self.publishing = False
                raise sqlite3.OperationalError("simulated admin upload COMMIT error")
            return underlying.commit()

        def __getattr__(self, name):
            return getattr(underlying, name)

    fault = FailPublication()
    store.db = fault
    with TestClient(app, raise_server_exceptions=False) as admin:
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        response = admin.post(
            f"/api/admin/songs/{song_id}/media",
            files={"file": ("new.mp4", b"new upload bytes", "video/mp4")},
        )

    assert fault.did_fail
    assert response.status_code >= 400
    saved = store.rows(
        "SELECT status,media_file,source_file FROM songs WHERE id=?", (song_id,)
    )[0]
    assert saved["status"] == initial_status
    assert saved["media_file"] == (original.name if initial_status == "ready" else None)
    assert saved["source_file"] == original.name
    assert original.is_file()
    assert len(list(store.media.iterdir())) == 1
    assert not underlying.in_transaction
