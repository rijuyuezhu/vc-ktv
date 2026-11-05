"""Stop subprocess trees spawned by one AutoPrep worker on Linux.

The server uses threads to prepare songs. Linux tracks children per native
thread, allowing cancellation of *one song's* subprocesses without killing
other workers or the web service. DB state controls what gets published.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path


def _children(pid: int, tid: int | None = None) -> list[int]:
    task = tid if tid is not None else pid
    try:
        return [
            int(x)
            for x in Path(f"/proc/{pid}/task/{task}/children").read_text().split()
        ]
    except (FileNotFoundError, OSError, ValueError):
        return []


def terminate_thread_children(native_tid: int) -> list[int]:
    """Kill the descendants of a worker thread, leaves its Python thread alive.

    Runs only after the corresponding song was marked cancelled. Direct
    children receive SIGKILL after their descendants to avoid orphaned GPU
    workers that could keep running if a parent process exits too early.
    """
    parent = os.getpid()
    first = _children(parent, native_tid)
    visited: set[int] = set()
    leaves_first: list[int] = []

    def visit(pid: int) -> None:
        if pid in visited or pid == parent:
            return
        visited.add(pid)
        for child in _children(pid):
            visit(child)
        leaves_first.append(pid)

    for child in first:
        visit(child)
    for pid in leaves_first:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return leaves_first
