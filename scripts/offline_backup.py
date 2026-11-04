"""Offline-only, verified VC-KTV data/config snapshots and empty-root restore.

Stop KTV and its workers before taking a snapshot. This tool never stops them.
Pipeline model assets are deliberately excluded and must be kept separately.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

SKIPPED_FILES = (".vc-ktv-pin-recovery.lock",)
SKIPPED_SUFFIXES = (".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal")


def _files(root: Path):
    return sorted(path for path in root.rglob("*") if path.is_file())


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_offline(data: Path) -> None:
    # MediaPins owns an exclusive flock for the lifetime of a running instance.
    # Older instances may not publish a lock; --service-stopped is still required.
    for marker in data.glob(".vc-ktv-pin-owner-*.lock"):
        fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "KTV media owner is running; stop all services"
                ) from error
        finally:
            os.close(fd)
    if any(data.rglob(".vc-ktv-inflight-*")):
        raise RuntimeError("Unfinished media links remain; review after stopping KTV")


def _copy(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"Refusing to follow symlink in backup data: {source}")
    if source.is_dir():
        target.mkdir(mode=0o700)
        for child in source.iterdir():
            if child.name in SKIPPED_FILES or child.name.startswith(
                ".vc-ktv-pin-owner-"
            ):
                continue
            if child.name.endswith(SKIPPED_SUFFIXES):
                continue  # SQLite backup API includes committed WAL transactions.
            _copy(child, target / child.name)
    elif source.is_file():
        if source.name == "room.sqlite3":
            with (
                closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db,
                closing(sqlite3.connect(target)) as snapshot,
            ):
                db.backup(snapshot)
                if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError(
                        f"SQLite snapshot failed integrity check: {source}"
                    )
        else:
            shutil.copyfile(source, target)
        target.chmod(0o600)
    else:
        raise ValueError(f"Unsupported backup item: {source}")


def _assert_data_pointer(data: Path) -> None:
    pointer = data / "active-session"
    if not pointer.is_file():
        return
    event = pointer.read_text().strip()
    if not event or "/" in event or "\\" in event or event in {".", ".."}:
        raise ValueError("Invalid active event pointer")
    if event != "legacy" and not all(
        (data / "sessions" / event / name).is_file()
        for name in ("info.json", "room.sqlite3")
    ):
        raise ValueError("Active event metadata or database missing")
    if event == "legacy" and not (data / "room.sqlite3").is_file():
        raise ValueError("Active legacy database missing")


def verify(snapshot: Path) -> dict:
    snapshot = snapshot.resolve()
    manifest = json.loads((snapshot / "manifest.json").read_text())
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unsupported backup manifest")
    expected = manifest["files"]
    if any(not name.startswith(("data/", "config/")) for name in expected):
        raise ValueError("Backup contains an unsupported file location")
    actual = {
        path.relative_to(snapshot).as_posix(): path
        for path in _files(snapshot)
        if path != snapshot / "manifest.json"
    }
    if set(actual) != set(expected):
        raise ValueError("Backup file inventory does not match manifest")
    if any(path.is_symlink() for path in snapshot.rglob("*")):
        raise ValueError("Backup contains symlink")
    for name, path in actual.items():
        record = expected[name]
        if path.stat().st_size != record["size"] or _hash(path) != record["sha256"]:
            raise ValueError(f"Backup file checksum mismatch: {name}")
        if path.name == "room.sqlite3":
            # Immutable read avoids creating -wal/-shm beside a sealed snapshot.
            with closing(
                sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)
            ) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError(f"Backup SQLite integrity failed: {name}")
    _assert_data_pointer(snapshot / "data")
    return manifest


def snapshot(data: Path, config: Path, output: Path) -> Path:
    data, config = data.resolve(), config.resolve()
    output = output.parent.resolve() / output.name
    if not data.is_dir() or not config.is_dir():
        raise FileNotFoundError("Data and config roots must already exist")
    if not all(
        (config / name).is_file() for name in ("admin-password", "token-secret")
    ):
        raise ValueError("Missing administrator credentials in config root")
    if (
        output.exists()
        or output.is_symlink()
        or output.is_relative_to(data)
        or output.is_relative_to(config)
    ):
        raise ValueError(
            "Backup destination must be new and outside source directories"
        )
    _check_offline(data)
    _assert_data_pointer(data)
    with tempfile.TemporaryDirectory(
        prefix=".vcktv-snapshot-", dir=output.parent
    ) as temp:
        stage = Path(temp)
        stage.chmod(0o700)
        (stage / "data").mkdir(mode=0o700)
        for child in data.iterdir():
            if child.name == "pipeline":  # Rebuildable, large CUDA assets.
                continue
            if child.name in SKIPPED_FILES or child.name.startswith(
                ".vc-ktv-pin-owner-"
            ):
                continue
            if child.name.endswith(SKIPPED_SUFFIXES):
                continue
            _copy(child, stage / "data" / child.name)
        _copy(config, stage / "config")
        manifest = {
            "schema": 1,
            "excluded": ["data/pipeline"],
            "files": {
                path.relative_to(stage).as_posix(): {
                    "size": path.stat().st_size,
                    "sha256": _hash(path),
                }
                for path in _files(stage)
            },
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (stage / "manifest.json").chmod(0o600)
        verify(stage)
        if output.exists() or output.is_symlink():
            raise FileExistsError(
                f"Snapshot destination appeared during backup: {output}"
            )
        stage.rename(output)
    return output


def restore(snapshot_dir: Path, data: Path, config: Path) -> None:
    snapshot_dir, data, config = (
        snapshot_dir.resolve(),
        data.parent.resolve() / data.name,
        config.parent.resolve() / config.name,
    )
    if data.exists() or config.exists() or data.is_symlink() or config.is_symlink():
        raise FileExistsError(
            "Restore requires two nonexistent destination directories"
        )
    if (
        data == config
        or data.is_relative_to(config)
        or config.is_relative_to(data)
        or data.is_relative_to(snapshot_dir)
        or config.is_relative_to(snapshot_dir)
    ):
        raise ValueError(
            "Restore destinations must be separate and outside the snapshot"
        )
    manifest = verify(snapshot_dir)
    created_data = created_config = False
    try:
        # mkdir is exclusive: a concurrent creator must not be deleted on error.
        data.mkdir(mode=0o700)
        created_data = True
        config.mkdir(mode=0o700)
        created_config = True
        shutil.copytree(snapshot_dir / "data", data, dirs_exist_ok=True)
        shutil.copytree(snapshot_dir / "config", config, dirs_exist_ok=True)
        for root in (data, config):
            for item in [root, *root.rglob("*")]:
                item.chmod(0o700 if item.is_dir() else 0o600)
        for name, record in manifest["files"].items():
            group, _, relative = name.partition("/")
            target = (data if group == "data" else config) / relative
            if (
                not target.is_file()
                or target.stat().st_size != record["size"]
                or _hash(target) != record["sha256"]
            ):
                raise ValueError(f"Restored file failed checksum: {name}")
        _assert_data_pointer(data)
    except BaseException:
        if created_data:
            shutil.rmtree(data, ignore_errors=True)
        if created_config:
            shutil.rmtree(config, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("snapshot")
    save.add_argument("--data", type=Path, required=True)
    save.add_argument("--config", type=Path, required=True)
    save.add_argument("--out", type=Path, required=True)
    save.add_argument("--service-stopped", action="store_true")
    check = commands.add_parser("verify")
    check.add_argument("--snapshot", type=Path, required=True)
    recover = commands.add_parser("restore")
    recover.add_argument("--snapshot", type=Path, required=True)
    recover.add_argument("--data", type=Path, required=True)
    recover.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "snapshot":
        if not args.service_stopped:
            parser.error(
                "Stop all KTV services/workers first and pass --service-stopped"
            )
        snapshot(args.data, args.config, args.out)
        print("Verified offline snapshot created")
    elif args.command == "verify":
        record = verify(args.snapshot)
        print(f"Verified {len(record['files'])} files and SQLite integrity")
    else:
        restore(args.snapshot, args.data, args.config)
        print("Restored into previously nonexistent data/config directories")


if __name__ == "__main__":
    main()
