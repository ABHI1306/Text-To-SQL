"""Per-user request limits: a sliding window kept in this process (the service runs one API)."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

WINDOW_SECONDS = 60.0

# Per user and route, per minute. Questions and SQL reruns use the local model and the warehouse.
ASK_PER_MINUTE = 30
RUN_NOW_PER_MINUTE = 10
VIEW_PER_MINUTE = 600


class SlidingWindowLimiter:
    def __init__(self, window_seconds: float = WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str, limit: int, now: float | None = None) -> float | None:
        """Record a hit; return None when allowed, else the seconds until a slot frees up."""
        current = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= current - self._window:
                hits.popleft()
            if len(hits) >= limit:
                return max(hits[0] + self._window - current, 0.0)
            hits.append(current)
            if len(self._hits) > 10_000:
                self._hits = {k: v for k, v in self._hits.items() if v}
            return None


limiter = SlidingWindowLimiter()
