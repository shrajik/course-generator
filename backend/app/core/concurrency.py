"""Adaptive concurrency limiter.

Fanning out chapter generation hits provider rate limits long before it hits any
limit in this code, so the limiter shrinks itself when the API returns 429 and
recovers slowly afterwards. Without this, higher concurrency just converts
latency into failures.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.core.logging import get_logger

log = get_logger(__name__)

RECOVERY_SECONDS = 30.0


class AdaptiveLimiter:
    """A semaphore whose effective size drops on rate limiting."""

    def __init__(self, limit: int, *, name: str = "limiter", minimum: int = 1) -> None:
        self.name = name
        self.limit = max(1, limit)
        self.minimum = max(1, min(minimum, self.limit))
        self._effective = self.limit
        self._semaphore = asyncio.Semaphore(self.limit)
        # Extra permits held back when the limiter has shrunk.
        self._parked = 0
        self._last_throttle = 0.0
        self._lock = asyncio.Lock()

    @property
    def effective_limit(self) -> int:
        return self._effective

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self._maybe_recover()
        await self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()

    async def throttled(self) -> None:
        """Called after a 429: halve the effective limit."""
        async with self._lock:
            if self._effective <= self.minimum:
                self._last_throttle = time.monotonic()
                return
            target = max(self.minimum, self._effective // 2)
            to_park = self._effective - target
            self._effective = target
            self._last_throttle = time.monotonic()
            log.warning(
                "%s: rate limited, reducing concurrency to %s", self.name, self._effective
            )
        # Park permits outside the lock so we never block other callers.
        for _ in range(to_park):
            await self._semaphore.acquire()
            self._parked += 1

    async def _maybe_recover(self) -> None:
        if self._parked == 0:
            return
        if time.monotonic() - self._last_throttle < RECOVERY_SECONDS:
            return
        async with self._lock:
            if self._parked == 0:
                return
            self._semaphore.release()
            self._parked -= 1
            self._effective += 1
            self._last_throttle = time.monotonic()
            log.info("%s: recovering, concurrency back to %s", self.name, self._effective)


_limiters: dict[str, AdaptiveLimiter] = {}


def get_limiter(name: str, limit: int) -> AdaptiveLimiter:
    """One limiter per phase name, shared across the process."""
    existing = _limiters.get(name)
    if existing is None or existing.limit != max(1, limit):
        existing = AdaptiveLimiter(limit, name=name)
        _limiters[name] = existing
    return existing


def reset_limiters() -> None:
    """Test helper."""
    _limiters.clear()


async def note_rate_limit(name: str) -> None:
    limiter = _limiters.get(name)
    if limiter is not None:
        await limiter.throttled()
