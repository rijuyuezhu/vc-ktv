"""Archived status corrections preserve both the CSV snapshot and real play times."""

import csv
import io
import sqlite3

from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app


def _archive(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"archive-status-consistency-test",
        auto_prepare=False,
    )
    return app


def _prepare(app, client, *, release=True):
    assert (
        client.post("/api/admin/login", json={"password": "admin"}).status_code == 200
    )
    event = client.post(
        "/api/admin/events", json={"name": "test room", "code": "123456"}
    ).json()
    with app.state.events.active().lock:
        db = app.state.events.active().db
        db.execute("INSERT INTO guests(id,name) VALUES ('guest','singer')")
        db.executemany(
            """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,
               requested_at,status,prep_state,played_at)
               VALUES ('BV0000000001',1,?,'artist','',60,'guest',?,?, 'done',?)""",
            [
                ("performed", 1, "played", 12345),
                ("not performed", 2, "cancelled", None),
            ],
        )
        db.commit()
    assert client.post("/api/admin/events/close").status_code == 200
    if release:
        assert (
            client.post(f"/api/admin/events/{event['id']}/release-media").status_code
            == 200
        )
    return event["id"], app.state.events.base / event["id"]


def _csv_songs(contents):
    return list(csv.DictReader(io.StringIO(contents.lstrip("\ufeff"))))


def test_failed_snapshot_write_rolls_back_archived_status(tmp_path, monkeypatch):
    app = _archive(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        event_id, folder = _prepare(app, client)
        endpoint = f"/api/admin/events/{event_id}/songs/1/status?status=skipped"
        csv_path = folder / "songs.csv"
        before = csv_path.read_bytes()
        write_atomic = app.state.events._write_atomic

        def fail_csv(path, content):
            if path == csv_path:
                raise OSError("simulated failed CSV write")
            return write_atomic(path, content)

        monkeypatch.setattr(app.state.events, "_write_atomic", fail_csv)
        assert client.put(endpoint).status_code == 500
        with sqlite3.connect(folder / "room.sqlite3") as db:
            assert db.execute(
                "SELECT status,played_at FROM songs WHERE id=1"
            ).fetchone() == ("played", 12345)
        assert csv_path.read_bytes() == before
        assert (
            client.get(f"/api/admin/events/{event_id}/songs.csv").text
            == before.decode()
        )

        monkeypatch.setattr(app.state.events, "_write_atomic", write_atomic)
        assert client.put(endpoint).status_code == 200
        assert (
            _csv_songs(csv_path.read_text(encoding="utf-8"))[0]["status"] == "skipped"
        )


def test_status_round_trip_preserves_real_time_and_frozen_order(tmp_path):
    app = _archive(tmp_path)
    with TestClient(app) as client:
        event_id, folder = _prepare(app, client)
        status_url = f"/api/admin/events/{event_id}/songs/1/status?status="
        detail_url = f"/api/events/{event_id}/songs/1"
        csv_url = f"/api/admin/events/{event_id}/songs.csv"
        original = _csv_songs(client.get(csv_url).text)
        assert original[0]["played_at"] == "12345"
        assert [row["title"] for row in original] == ["performed", "not performed"]

        assert client.put(status_url + "closed_unplayed").status_code == 200
        assert client.get(detail_url).json()["song"]["played_at"] is None
        assert (
            client.get(f"/api/admin/events/{event_id}/songs").json()["songs"][0][
                "played_at"
            ]
            is None
        )
        snapshot = _csv_songs((folder / "songs.csv").read_text(encoding="utf-8"))
        assert snapshot == _csv_songs(client.get(csv_url).text)
        assert snapshot[0]["played_at"] == ""
        assert snapshot[0]["status"] == "closed_unplayed"
        assert client.put(status_url + "played").status_code == 200
        assert client.get(detail_url).json()["song"]["played_at"] == 12345
        restored = _csv_songs(client.get(csv_url).text)
        assert restored == _csv_songs(
            (folder / "songs.csv").read_text(encoding="utf-8")
        )
        assert restored[0]["played_at"] == "12345"
        assert [row["title"] for row in restored] == ["performed", "not performed"]
        with sqlite3.connect(folder / "room.sqlite3") as db:
            assert db.execute(
                "SELECT played_at,archive_order FROM songs WHERE id=1"
            ).fetchone() == (12345, 1)

        assert (
            client.put(
                f"/api/admin/events/{event_id}/songs/2/status?status=played"
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/events/{event_id}/songs/2").json()["song"]["played_at"]
            is None
        )
        no_fabrication = _csv_songs((folder / "songs.csv").read_text(encoding="utf-8"))
        assert no_fabrication[1]["played_at"] == ""


def test_edit_without_saved_csv_keeps_archive_readable(tmp_path):
    app = _archive(tmp_path)
    with TestClient(app) as client:
        event_id, folder = _prepare(app, client, release=False)
        assert not (folder / "songs.csv").exists()
        assert (
            client.put(
                f"/api/admin/events/{event_id}/songs/1/status?status=skipped"
            ).status_code
            == 200
        )
        assert not (folder / "songs.csv").exists()
        records = _csv_songs(client.get(f"/api/admin/events/{event_id}/songs.csv").text)
        assert (records[0]["status"], records[0]["played_at"]) == ("skipped", "12345")
