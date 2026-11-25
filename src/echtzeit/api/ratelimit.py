"""A token bucket per client. In memory, so it limits one server process only."""

import math
import threading
import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        if capacity <= 0 or refill_per_second <= 0:
            raise ValueError("capacity and refill_per_second must be positive")
        self._capacity = capacity
        self._rate = refill_per_second
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> float:
        """Take one token. Returns 0 if allowed, else the seconds to wait before retrying."""
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(key, (float(self._capacity), now))
            tokens = min(self._capacity, tokens + (now - last) * self._rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return math.ceil((1 - tokens) / self._rate * 1000) / 1000
