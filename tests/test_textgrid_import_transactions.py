"""TextGrid failure after archive publication must leave no partial lyrics state."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_textgrid_import import grid_file
from vc_ktv.application.factory import create_app


class ImportFault:
    def __init__(self, db, fault):
        self.db = db
        self.fault = fault
        self.failures = 0
        self.rollbacks = 0

    def execute(self, sql, *args):
        if (
            self.fault == "update"
            and "UPDATE songs SET lyrics=?" in sql
            and not self.failures
        ):
            self.failures += 1
            raise sqlite3.OperationalError("injected TextGrid UPDATE failure")
        return self.db.execute(sql, *args)

    def commit(self):
        if self.fault == "commit" and not self.failures:
            self.failures += 1
            raise sqlite3.OperationalError("injected TextGrid COMMIT failure")
        return self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.db.rollback()

    def __getattr__(self, name):
        return getattr(self.db, name)


@pytest.mark.parametrize("fault", ("update", "commit"))
def test_failed_textgrid_publication_keeps_lyrics_and_archive_consistent(
    tmp_path, fault
):
    app = create_app(
        root=tmp_path / "room",
        password="test-pass",
        secret=b"textgrid-fault-key",
        auto_prepare=False,
    )
    app.state.events.create("测试", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "测试",
            "uploader": "测试",
            "cover": "",
            "duration": 4,
        }

    app.state.lookup = lookup
    app.state.probe = lambda _file: 4
    store = app.state.store
    grid = grid_file(tmp_path / "aligned.TextGrid").read_bytes()
    with (
        TestClient(app, raise_server_exceptions=False) as guest,
        TestClient(app, raise_server_exceptions=False) as admin,
    ):
        join_and_register(guest)
        song_id = guest.post("/api/songs", json={"bvid": "BV0000000003"}).json()["id"]
        assert (
            admin.post("/api/admin/login", json={"password": "test-pass"}).status_code
            == 200
        )
        assert (
            admin.post(
                f"/api/admin/songs/{song_id}/media",
                files={"file": ("source.mp4", b"test-media", "video/mp4")},
            ).status_code
            == 200
        )
        assert (
            admin.post(
                f"/api/admin/songs/{song_id}/lyrics",
                json={"text": "你，好!\n下一句"},
            ).status_code
            == 200
        )
        before = store.rows("SELECT id,lyrics FROM songs WHERE id=?", (song_id,))
        db = store.db
        bad = ImportFault(db, fault)
        store.db = bad
        try:
            result = admin.post(
                f"/api/admin/songs/{song_id}/alignment/import",
                files={"file": ("alignment.TextGrid", grid, "text/plain")},
            )
        finally:
            store.db = db
        assert result.status_code == 500
        assert bad.failures == 1
        assert bad.rollbacks == 1
        assert not db.in_transaction
        assert (
            store.rows("SELECT id,lyrics FROM songs WHERE id=?", (song_id,)) == before
        )
        assert not list(store.media.glob(f"{song_id}-alignment-*"))

        retried = admin.post(
            f"/api/admin/songs/{song_id}/alignment/import",
            files={"file": ("alignment.TextGrid", grid, "text/plain")},
        )
        assert retried.status_code == 200, retried.text
        assert len(list(store.media.glob(f"{song_id}-alignment-*"))) == 1
