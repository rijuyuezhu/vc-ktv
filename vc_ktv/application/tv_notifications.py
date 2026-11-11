"""Process-local SSE wakeups; SQLite remains the authority for commands and ACKs."""

import asyncio
from collections import defaultdict
from contextlib import contextmanager, suppress
from threading import Lock


class TVNotifications:
    def __init__(self):
        self._lock = Lock()
        self._listeners = defaultdict(set)

    @contextmanager
    def listen(self, channel: str):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=1)
        subscriber = (loop, queue)
        with self._lock:
            self._listeners[channel].add(subscriber)
        try:
            yield queue
        finally:
            with self._lock:
                self._listeners[channel].discard(subscriber)

    def notify(self, channel: str) -> None:
        with self._lock:
            listeners = tuple(self._listeners[channel])
        for loop, queue in listeners:
            # A browser disconnect may coincide with server loop shutdown.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._wake, queue)

    @staticmethod
    def _wake(queue: asyncio.Queue) -> None:
        if queue.empty():
            queue.put_nowait(True)
