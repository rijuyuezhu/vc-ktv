"""Cleanup of abandoned media pins must not touch another live owner."""

import os
from pathlib import Path

import pytest

from vc_ktv.application.factory import create_app
from vc_ktv.infra.media_pins import MediaPins, fcntl

pytestmark = pytest.mark.skipif(fcntl is None, reason="POSIX advisory locks required")


def test_new_app_recovers_crashed_owner_without_touching_media(tmp_path):
    root = tmp_path / "ktv"
    root.mkdir()
    media = root / "sessions" / "past" / "media"
    media.mkdir(parents=True)
    original = media / "original.mp4"
    original.write_bytes(b"retained")
    orphan_owner = MediaPins(root)
    orphan = orphan_owner.pin(original)
    assert orphan.read_bytes() == b"retained"
    orphan_owner.close()  # Equivalent to the lock released by process exit.

    app = create_app(
        root=root,
        password="test-admin",
        secret=b"media-pin-test-secret",
        auto_prepare=False,
    )
    assert not orphan.exists()
    assert original.read_bytes() == b"retained"
    assert not (root / f".vc-ktv-pin-owner-{orphan_owner.token}.lock").exists()
    app.state.media_pins.close()


def test_live_owner_is_never_reclaimed_by_another_app(tmp_path):
    root = tmp_path / "ktv"
    root.mkdir()
    media = root / "media"
    media.mkdir()
    original = media / "recording.mp4"
    original.write_bytes(b"still-live")
    first = MediaPins(root)
    pinned = first.pin(original)

    second = MediaPins(root)
    assert pinned.exists()
    assert pinned.read_bytes() == b"still-live"
    assert (root / f".vc-ktv-pin-owner-{first.token}.lock").exists()
    second.close()

    first.close()
    third = MediaPins(root)
    assert not pinned.exists()
    assert original.read_bytes() == b"still-live"
    third.close()


def test_unknown_legacy_inflight_links_are_not_deleted_without_owner(tmp_path):
    root = tmp_path / "ktv"
    root.mkdir()
    media = root / "media"
    media.mkdir()
    original = media / "old.mp4"
    original.write_bytes(b"old")
    legacy = media / ".vc-ktv-inflight-0123456789abcdef0123.mp4"
    os.link(original, legacy)

    manager = MediaPins(root)
    assert legacy.read_bytes() == b"old"  # Cannot prove legacy owner is dead.
    assert original.exists()
    manager.close()


def test_abrupt_subprocess_exit_releases_owner_for_recovery(tmp_path):
    import subprocess
    import sys

    root = tmp_path / "ktv"
    root.mkdir()
    media = root / "media"
    media.mkdir()
    original = media / "source.mp4"
    original.write_bytes(b"source")

    child = """
import os
import sys
from pathlib import Path
from vc_ktv.infra.media_pins import MediaPins
root = Path(sys.argv[1])
owner = MediaPins(root)
owner.pin(root / "media" / "source.mp4")
os._exit(9)
"""
    process = subprocess.run(
        [sys.executable, "-c", child, str(root)],
        cwd=Path(__file__).resolve().parents[1],
        timeout=5,
        check=False,
    )
    assert process.returncode == 9
    assert list(media.glob(".vc-ktv-inflight-*"))
    current = MediaPins(root)
    assert not list(media.glob(".vc-ktv-inflight-*"))
    assert original.read_bytes() == b"source"
    current.close()
