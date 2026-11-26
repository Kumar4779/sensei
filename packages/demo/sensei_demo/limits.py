"""Public-abuse limits for the hosted demo (Demo Slice 2).

  - DailySpend: a global ceiling on estimated provider spend per UTC day, across
    every session and every server run. The counter is persisted (a 0600 JSON
    file, replaced atomically), so a restart or redeploy can't reset it; it rolls
    over when the UTC date changes. It sits on top of the per-run cap.
  - RateLimiter: a sliding window per client IP, for new sessions and for
    utterances.
  - client_ip: the caller's address. A proxy header (Fly-Client-IP on Fly.io) is
    trusted only when configured, because a client could set it on a direct
    connection.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from starlette.requests import Request


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class DailySpend:
    """Estimated spend today (UTC), persisted across restarts, with a ceiling."""

    def __init__(self, path: str | Path, ceiling_usd: float, *, today: Callable[[], str] = utc_today) -> None:
        self.path = Path(path)
        self.ceiling_usd = ceiling_usd
        self._today = today
        self._lock = threading.Lock()
        self._date, self._usd = self._load()

    def _load(self) -> tuple[str, float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return str(data["date"]), float(data["usd"])
        except (OSError, ValueError, KeyError, TypeError):
            return self._today(), 0.0  # no file yet (or unreadable): start today at zero

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".spend-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"date": self._date, "usd": round(self._usd, 6)}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)  # atomic: a crash leaves the old count or the new one, never half
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _roll(self) -> None:
        today = self._today()
        if today != self._date:
            self._date, self._usd = today, 0.0

    def spent(self) -> float:
        with self._lock:
            self._roll()
            return round(self._usd, 4)

    def allows(self) -> bool:
        return self.spent() < self.ceiling_usd

    def add(self, usd: float) -> float:
        if usd <= 0:
            return self.spent()
        with self._lock:
            self._roll()
            self._usd += usd
            self._save()
            return round(self._usd, 4)


class RateLimiter:
    """At most `limit` events per `window` seconds per key (a sliding window)."""

    def __init__(self, limit: int, window: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Record an event for `key` and return None if allowed, or the seconds until one would be allowed."""
        now = self._clock()
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - self.window:
                events.popleft()
            if len(events) >= self.limit:
                return max(0.0, events[0] + self.window - now)
            events.append(now)
            if len(self._events) > 10_000:  # bound memory: drop keys with nothing in the window
                for k in [k for k, v in self._events.items() if not v]:
                    del self._events[k]
            return None


def client_ip(request: Request, trusted_header: str | None) -> str:
    if trusted_header:
        value = request.headers.get(trusted_header, "").split(",")[0].strip()
        if value:
            return value
    return request.client.host if request.client else "unknown"
