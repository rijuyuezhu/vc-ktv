"""Crash-safe ownership of temporary media response hard links.

A pinned file may outlive its HTTP request only if the process crashes before
its response finally block. Owners hold advisory locks so another app instance
never removes a link belonging to a still-running instance.
"""

import os
import re
import secrets
import weakref
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: keep streaming support, skip unsafe auto-recovery.
    fcntl = None

_OWNER = re.compile(r"\.vc-ktv-pin-owner-([a-f0-9]{20})\.lock")


class MediaPins:
    def __init__(self, root: Path):
        self.root = root
        self.token = secrets.token_hex(10)
        self._close_owner = None
        if fcntl is None:
            return

        # Serialize startup discovery and owner publication between processes.
        gate = os.open(
            root / ".vc-ktv-pin-recovery.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(gate, fcntl.LOCK_EX)
            owner = os.open(
                root / f".vc-ktv-pin-owner-{self.token}.lock",
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
            fcntl.flock(owner, fcntl.LOCK_EX)
            self._close_owner = weakref.finalize(self, os.close, owner)
            self._recover()
        finally:
            os.close(gate)

    def _recover(self):
        for marker in self.root.glob(".vc-ktv-pin-owner-*.lock"):
            match = _OWNER.fullmatch(marker.name)
            if not match or match[1] == self.token:
                continue
            try:
                fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # The owner (possibly another server) is alive.
                for pinned in self.root.rglob(f".vc-ktv-inflight-{match[1]}-*"):
                    pinned.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
            finally:
                os.close(fd)

    def pin(self, path: Path) -> Path:
        pinned = path.with_name(
            f".vc-ktv-inflight-{self.token}-{secrets.token_hex(10)}{path.suffix}"
        )
        os.link(path, pinned)
        return pinned

    def close(self):
        """Release ownership for tests and graceful application disposal."""
        if self._close_owner is not None:
            self._close_owner()
