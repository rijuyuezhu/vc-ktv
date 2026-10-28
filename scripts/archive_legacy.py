#!/usr/bin/env python3
"""Archive the closed pre-session database and media in one directory."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path


def archive(root: Path) -> Path:
    root = root.expanduser().resolve()
    original = root / "room.sqlite3"
    marker = root / "legacy.closed"
    dest = root / "sessions" / "legacy"
    staging = root / "sessions" / ".legacy-archive-in-progress"
    if not original.is_file() or not marker.is_file():
        raise RuntimeError(
            "No closed legacy event found; close it through /admin/ first"
        )
    if dest.exists() or staging.exists():
        raise RuntimeError("Legacy archive already exists or migration is incomplete")
    if (root / "active-session").exists() and (
        root / "active-session"
    ).read_text().strip() == "legacy":
        raise RuntimeError("Legacy event is still active")

    staging.mkdir(mode=0o700, parents=True)
    try:
        with (
            sqlite3.connect(original) as source,
            sqlite3.connect(staging / "room.sqlite3") as target,
        ):
            source.backup(target)
            assert target.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            for table in ("guests", "songs"):
                count = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert (
                    target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    == count
                )
        media = root / "media"
        if media.exists():
            shutil.copytree(media, staging / "media")
            originals = sorted(
                (p.relative_to(media), p.stat().st_size)
                for p in media.rglob("*")
                if p.is_file()
            )
            copies = sorted(
                (p.relative_to(staging / "media"), p.stat().st_size)
                for p in (staging / "media").rglob("*")
                if p.is_file()
            )
            if originals != copies:
                raise RuntimeError("Media copy verification failed")
        (staging / "info.json").write_text(
            json.dumps(
                {
                    "id": "legacy",
                    "name": "原有 KTV",
                    "status": "closed",
                    "legacy": True,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        (staging / "info.json").chmod(0o600)
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    for filename in ("room.sqlite3", "room.sqlite3-wal", "room.sqlite3-shm"):
        (root / filename).unlink(missing_ok=True)
    if (root / "media").exists():
        shutil.rmtree(root / "media")
    marker.unlink()
    return dest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        print(f"Archived legacy KTV to {archive(args.root)}")
    else:
        print(
            "Dry run. Legacy data:",
            (args.root / "room.sqlite3").exists(),
            "closed:",
            (args.root / "legacy.closed").exists(),
        )
        print("Use --apply after ending legacy KTV and stopping other DB users.")
