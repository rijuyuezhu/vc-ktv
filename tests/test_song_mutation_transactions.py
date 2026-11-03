"""Song submission and lyric controls must rollback SQLite mutations on failure."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_guest_skip_lyrics import new_app


class OneSqlFault:
    def __init__(self, connection, fault, statement):
        self.connection = connection
        self.fault = fault
        self.statement = statement
        self.failures = 0
        self.rollbacks = 0

    def execute(self, sql, *args):
        if self.fault == "execute" and self.statement in sql and not self.failures:
            self.failures += 1
            raise sqlite3.OperationalError("injected song state UPDATE/INSERT fault")
        return self.connection.execute(sql, *args)

    def commit(self):
        if self.fault == "commit" and not self.failures:
            self.failures += 1
            raise sqlite3.OperationalError("injected song state COMMIT fault")
        return self.connection.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.connection.rollback()

    def __getattr__(self, attr):
        return getattr(self.connection, attr)


@pytest.mark.parametrize("fault", ("execute", "commit"))
@pytest.mark.parametrize(
    "kind,statement",
    (
        ("submit", "INSERT INTO songs("),
        ("guest_skip", "UPDATE songs SET skip_subtitles=1"),
        ("guest_upload", "UPDATE songs SET lyric_choice=?"),
        ("admin_retry", "UPDATE songs SET prep_state='downloaded'"),
        ("admin_skip", "UPDATE songs SET media_file=source_file"),
        ("admin_lyrics", "UPDATE songs SET lyrics=?"),
    ),
)
def test_song_mutation_failure_is_transactional_and_retryable(
    tmp_path, kind, statement, fault
):
    app = new_app(tmp_path)
    store = app.state.store
    with (
        TestClient(app, raise_server_exceptions=False) as guest,
        TestClient(app, raise_server_exceptions=False) as admin,
    ):
        join_and_register(guest, "Song owner")
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )

        if kind != "submit":
            created = guest.post("/api/songs", json={"bvid": "BV0000000001"})
            assert created.status_code == 200, created.text
            song_id = created.json()["id"]
            if kind in ("admin_retry", "admin_skip"):
                source = store.media / f"source-{song_id}.mp4"
                source.write_bytes(b"untouched-source")
                with store.lock:
                    store.db.execute(
                        "UPDATE songs SET status='ready',source_file=?,media_file=?,"
                        "prep_state=?,lyric_choice=? WHERE id=?",
                        (
                            source.name,
                            source.name,
                            "original" if kind == "admin_retry" else "downloaded",
                            "first line\nsecond line",
                            song_id,
                        ),
                    )
                    store.db.commit()
        else:
            song_id = None

        before = store.rows(
            "SELECT id,status,prep_state,lyric_choice,lyric_source,skip_subtitles,"
            "prep_stage,prep_error,lyrics,source_file,media_file FROM songs ORDER BY id"
        )
        lrc = b"[00:01.00]first line has words\n[00:02.00]second line has words\n"
        routes = {
            "submit": (guest, "/api/songs", {"json": {"bvid": "BV0000000001"}}),
            "guest_skip": (guest, f"/api/songs/{song_id}/lyrics/skip", {}),
            "guest_upload": (
                guest,
                f"/api/songs/{song_id}/lyrics/upload",
                {"files": {"file": ("lyrics.lrc", lrc, "text/plain")}},
            ),
            "admin_retry": (
                admin,
                f"/api/admin/songs/{song_id}/subtitles/retry",
                {},
            ),
            "admin_skip": (
                admin,
                f"/api/admin/songs/{song_id}/subtitles/skip",
                {},
            ),
            "admin_lyrics": (
                admin,
                f"/api/admin/songs/{song_id}/lyrics",
                {"json": {"text": "manually edited lyrics"}},
            ),
        }
        client, route, kwargs = routes[kind]
        real_db = store.db
        injected = OneSqlFault(real_db, fault, statement)
        store.db = injected
        try:
            failed = client.post(route, **kwargs)
        finally:
            store.db = real_db
        assert failed.status_code == 500, f"{kind} {fault}: {failed.text[:120]}"
        assert injected.failures == 1
        assert injected.rollbacks == 1
        assert not real_db.in_transaction
        assert (
            store.rows(
                "SELECT id,status,prep_state,lyric_choice,lyric_source,skip_subtitles,"
                "prep_stage,prep_error,lyrics,source_file,media_file FROM songs ORDER BY id"
            )
            == before
        )

        retried = client.post(route, **kwargs)
        assert retried.status_code == 200, f"{kind} {fault}: {retried.text[:120]}"
        if kind == "submit":
            assert len(store.rows("SELECT id FROM songs")) == 1
        elif kind == "guest_skip":
            assert (
                store.rows("SELECT skip_subtitles FROM songs WHERE id=?", (song_id,))[
                    0
                ]["skip_subtitles"]
                == 1
            )
        elif kind == "guest_upload":
            assert (
                store.rows("SELECT lyric_source FROM songs WHERE id=?", (song_id,))[0][
                    "lyric_source"
                ]
                == "上传 LRC"
            )
        elif kind == "admin_retry":
            assert (
                store.rows("SELECT prep_state FROM songs WHERE id=?", (song_id,))[0][
                    "prep_state"
                ]
                == "downloaded"
            )
        elif kind == "admin_skip":
            assert (
                store.rows("SELECT skip_subtitles FROM songs WHERE id=?", (song_id,))[
                    0
                ]["skip_subtitles"]
                == 1
            )
        else:
            assert (
                store.rows("SELECT lyrics FROM songs WHERE id=?", (song_id,))[0][
                    "lyrics"
                ]
                == "manually edited lyrics"
            )
