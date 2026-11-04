"""Offline event/media/config backup, tamper detection and empty-root recovery."""

import fcntl
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from scripts import offline_backup
from scripts.offline_backup import restore, snapshot, verify
from vc_ktv.app import Store, create_app


def fixture_roots(tmp_path):
    data = tmp_path / "source-data"
    config = tmp_path / "source-config"
    data.mkdir(mode=0o700)
    config.mkdir(mode=0o700)
    (config / "admin-password").write_text("test-only-secret")
    (config / "token-secret").write_text("abcdef0123456789")
    (config / "other-setting").write_text("custom")
    (data / "pipeline").mkdir()
    (data / "pipeline" / "model.bin").write_bytes(b"model-not-backed-up")
    (data / "qpool-example").mkdir()
    (data / "qpool-example" / "note.txt").write_text("other-private-data")
    for name, status in [("first-event", "closed"), ("second-event", "active")]:
        event = data / "sessions" / name
        event.mkdir(parents=True)
        (event / "info.json").write_text(
            json.dumps({"id": name, "name": "测试", "status": status, "code": "123456"})
        )
        store = Store(event)
        store.db.execute("INSERT INTO guests(id,name) VALUES('u','访客')")
        store.db.commit()
        (store.media / "song.mp4").write_bytes(
            b"original-private-video" + name.encode()
        )
        store.db.close()
    (data / "active-session").write_text("second-event\n")
    return data, config


def test_snapshot_restore_all_events_media_config_and_new_old_app(tmp_path):
    data, config = fixture_roots(tmp_path)
    backup = tmp_path / "backup"
    snapshot(data, config, backup)
    assert not (backup / "data/pipeline").exists()
    assert (backup / "data/qpool-example/note.txt").read_text() == "other-private-data"
    assert len(verify(backup)["files"]) > 7
    restored_data, restored_config = (
        tmp_path / "restore-data",
        tmp_path / "restore-config",
    )
    restore(backup, restored_data, restored_config)
    assert restored_config.joinpath("admin-password").read_text() == "test-only-secret"
    assert restored_config.joinpath("token-secret").stat().st_mode & 0o077 == 0
    for event in ("first-event", "second-event"):
        assert (
            restored_data / "sessions" / event / "media/song.mp4"
        ).read_bytes() == b"original-private-video" + event.encode()
        with sqlite3.connect(restored_data / "sessions" / event / "room.sqlite3") as db:
            assert db.execute("SELECT name FROM guests").fetchone()[0] == "访客"

    app = create_app(
        root=restored_data,
        password="test-only-secret",
        secret=b"test-restored-token",
        auto_prepare=False,
    )
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"]
        assert client.get("/api/event").json()["event"]["id"] == "second-event"
    assert data.joinpath("sessions", "second-event", "room.sqlite3").exists()
    assert not list(backup.rglob("*.sqlite3-wal"))
    assert not list(backup.rglob("*.sqlite3-shm"))
    assert verify(backup) == verify(backup)  # Verification itself creates no WAL.


def test_legacy_database_wal_backup_and_restore(tmp_path):
    data, config = fixture_roots(tmp_path)
    (data / "active-session").unlink()
    legacy = Store(data)
    legacy.db.execute("INSERT INTO guests(id,name) VALUES('old','legacy')")
    legacy.db.commit()
    # Keep WAL uncheckpointed to verify the SQLite backup API, not file copying.
    backup = snapshot(data, config, tmp_path / "legacy-backup")
    assert not (backup / "data/room.sqlite3-wal").exists()
    with sqlite3.connect(backup / "data/room.sqlite3") as db:
        assert (
            db.execute("SELECT name FROM guests WHERE id='old'").fetchone()[0]
            == "legacy"
        )
    legacy.db.close()


def test_backup_detects_modified_media_and_fails_closed(tmp_path):
    data, config = fixture_roots(tmp_path)
    backup = snapshot(data, config, tmp_path / "backup")
    (backup / "data/sessions/first-event/media/song.mp4").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify(backup)
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore(backup, tmp_path / "restore-data", tmp_path / "restore-config")
    assert not (tmp_path / "restore-data").exists()


def test_restore_refuses_existing_destinations(tmp_path):
    data, config = fixture_roots(tmp_path)
    backup = snapshot(data, config, tmp_path / "backup")
    target = tmp_path / "nonempty"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("unchanged")
    with pytest.raises(FileExistsError, match="nonexistent"):
        restore(backup, target, tmp_path / "config-destination")
    assert sentinel.read_text() == "unchanged"
    with pytest.raises(ValueError, match="separate"):
        restore(backup, tmp_path / "new-data", tmp_path / "new-data/config")
    assert not (tmp_path / "new-data").exists()


def test_snapshot_never_follows_symlinks(tmp_path):
    data, config = fixture_roots(tmp_path)
    external = tmp_path / "external"
    external.write_text("do-not-disclose")
    (data / "sessions/second-event/media/unsafe.mp4").symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        snapshot(data, config, tmp_path / "bad-backup")
    assert not (tmp_path / "bad-backup").exists()


def test_backup_rejects_live_owner_and_temp_media_link(tmp_path):
    data, config = fixture_roots(tmp_path)
    owner = data / ".vc-ktv-pin-owner-0123456789abcdefabcd.lock"
    owner.touch()
    with owner.open() as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="running"):
            snapshot(data, config, tmp_path / "backup")
    (data / "sessions/second-event/media/.vc-ktv-inflight-abc.mp4").write_bytes(b"pin")
    with pytest.raises(RuntimeError, match="Unfinished"):
        snapshot(data, config, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_bad_event_pointer_and_nested_destinations_fail_without_writes(tmp_path):
    data, config = fixture_roots(tmp_path)
    (data / "active-session").write_text("../../elsewhere")
    with pytest.raises(ValueError, match="Invalid active"):
        snapshot(data, config, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    (data / "active-session").write_text("second-event")
    with pytest.raises(ValueError, match="outside source"):
        snapshot(data, config, data / "backup")
    (data / "sessions/second-event/room.sqlite3").unlink()
    with pytest.raises(ValueError, match="database missing"):
        snapshot(data, config, tmp_path / "incomplete")


def test_nested_manifest_file_and_failed_restore_inventory(tmp_path):
    data, config = fixture_roots(tmp_path)
    (data / "qpool-example/manifest.json").write_text('{"not":"the backup manifest"}')
    backup = snapshot(data, config, tmp_path / "backup")
    assert (backup / "data/qpool-example/manifest.json").is_file()
    verify(backup)
    new_file = backup / "data/new-unauthorized.txt"
    new_file.write_text("not in manifest")
    with pytest.raises(ValueError, match="inventory"):
        verify(backup)


def test_restore_fails_closed_if_copied_media_changes(tmp_path, monkeypatch):
    data, config = fixture_roots(tmp_path)
    backup = snapshot(data, config, tmp_path / "backup")
    original = offline_backup.shutil.copytree

    def sabotage(source, target, *args, **kwargs):
        result = original(source, target, *args, **kwargs)
        if source == backup / "data":
            (target / "sessions/second-event/media/song.mp4").write_bytes(b"corrupt")
        return result

    monkeypatch.setattr(offline_backup.shutil, "copytree", sabotage)
    with pytest.raises(ValueError, match="Restored file failed checksum"):
        restore(backup, tmp_path / "target-data", tmp_path / "target-config")
    assert not (tmp_path / "target-data").exists()
    assert not (tmp_path / "target-config").exists()


def test_sqlite_source_uri_handles_spaces_and_special_characters(tmp_path):
    nested = tmp_path / "fixture with spaces and #hash"
    nested.mkdir()
    data, config = fixture_roots(nested)
    backup = snapshot(data, config, nested / "offline snapshot")
    verify(backup)
    restore(backup, nested / "new data", nested / "new config")
    assert (nested / "new data/sessions/second-event/room.sqlite3").is_file()


def test_concurrent_restore_destination_is_not_deleted(tmp_path, monkeypatch):
    data, config = fixture_roots(tmp_path)
    backup = snapshot(data, config, tmp_path / "backup")
    target_data, target_config = tmp_path / "new-data", tmp_path / "new-config"
    original_mkdir = type(target_config).mkdir

    def race(self, *args, **kwargs):
        if self == target_config:
            original_mkdir(self, *args, **kwargs)
            (self / "other-process.txt").write_text("keep")
            raise FileExistsError("Concurrent config destination creator")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(type(target_config), "mkdir", race)
    with pytest.raises(FileExistsError, match="Concurrent"):
        restore(backup, target_data, target_config)
    assert not target_data.exists()
    assert (target_config / "other-process.txt").read_text() == "keep"


def test_snapshot_cli_requires_explicit_offline_acknowledgement(tmp_path):
    data, config = fixture_roots(tmp_path)
    command = [
        sys.executable,
        "-m",
        "scripts.offline_backup",
        "snapshot",
        "--data",
        str(data),
        "--config",
        str(config),
        "--out",
        str(tmp_path / "backup"),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert result.returncode != 0
    assert "--service-stopped" in result.stderr
    assert not (tmp_path / "backup").exists()
