"""Stop subprocess trees spawned by one AutoPrep worker on Linux.

The server uses threads to prepare songs. Linux tracks children per native
thread, allowing cancellation of *one song's* subprocesses without killing
other workers or the web service. DB state controls what gets published.
"""

from __future__ import annotations

import contextlib
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

    Called for explicit song cancellation or a verified externally stalled
    subprocess. The caller chooses whether to cancel or record a retryable
    failure. Kill descendants before direct children to avoid orphaned GPU
    workers when their parent exits.
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
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    return leaves_first


def thread_external_activity(
    native_tid: int,
) -> tuple[tuple[int, int, int, int], ...] | None:
    """Return external subprocess identity, CPU and I/O progress or unknown.

    If a process cannot be inspected (exited mid-sample, permissions, /proc
    disabled), return None: never kill a legitimate worker based on uncertainty.
    Empty tuples mean no external child process to supervise.
    """
    owner = os.getpid()
    first = _children(owner, native_tid)
    visited = set()
    activity = []
    stack = list(first)
    while stack:
        pid = stack.pop()
        if pid in visited or pid == owner:
            continue
        visited.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            start_ticks = int(stat[19])  # /proc stat field 22 (starttime).
            cpu_ticks = int(stat[11]) + int(stat[12])  # utime + stime.
            lines = Path(f"/proc/{pid}/io").read_text().splitlines()
            metrics = dict(line.split(":", 1) for line in lines)
            io_bytes = sum(
                int(metrics[key])
                for key in ("rchar", "wchar", "read_bytes", "write_bytes")
            )
            stack.extend(_children(pid))
        except (OSError, IndexError, KeyError, ValueError):
            return None
        activity.append((pid, start_ticks, cpu_ticks, io_bytes))
    return tuple(sorted(activity))
