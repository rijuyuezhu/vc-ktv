"""Per-event aggregate disk budget, counting all files under the event directory."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

EVENT_DISK_BUDGET_BYTES = 200 * 1024**3


class DiskBudgetExceeded(RuntimeError):
    def __init__(self):
        super().__init__("本场磁盘空间已达 200 GiB，请清理本场媒体后再试")


def directory_size(root: Path) -> int:
    """Count file sizes including intermediate artifacts, but never follow symlinks."""
    total = 0
    pending = [root]
    while pending:
        path = pending.pop()
        try:
            with os.scandir(path) as entries:
                for item in entries:
                    try:
                        if item.is_dir(follow_symlinks=False):
                            pending.append(Path(item.path))
                        elif item.is_file(follow_symlinks=False):
                            total += item.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue  # Concurrent cleanup or atomic rename.
        except FileNotFoundError:
            continue
    return total


class EventDiskBudget:
    def __init__(self, root: Path, *, limit_bytes: int = EVENT_DISK_BUDGET_BYTES):
        self.root = Path(root)
        self.limit_bytes = limit_bytes

    def used_bytes(self) -> int:
        return directory_size(self.root)

    def check(self, *, additional_bytes: int = 0) -> None:
        if self.used_bytes() + additional_bytes > self.limit_bytes:
            raise DiskBudgetExceeded()


# Workers write source media in chunks. Share this event-scoped quota without
# adding optional budget arguments to every downloader and renderer.
_active = threading.local()


class WorkCancelled(RuntimeError):
    pass


@contextmanager
def budget_scope(
    budget: EventDiskBudget | None, cancelled: threading.Event | None = None
):
    previous = getattr(_active, "budget", None)
    previous_cancelled = getattr(_active, "cancelled", None)
    _active.budget = budget
    _active.cancelled = cancelled
    try:
        yield
    finally:
        _active.budget = previous
        _active.cancelled = previous_cancelled


def check_current_budget() -> None:
    cancelled = getattr(_active, "cancelled", None)
    if cancelled is not None and cancelled.is_set():
        raise WorkCancelled("歌曲制作已取消")
    budget = getattr(_active, "budget", None)
    if budget is not None:
        budget.check()
