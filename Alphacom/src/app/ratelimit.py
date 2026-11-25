"""In-memory token buckets. Per-process, so N replicas means N x the limit; in a real
deployment this moves to the gateway or Redis (see DECISIONS.md)."""

import math
import time
from dataclasses import dataclass


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_s: int


class TokenBucket:
    def __init__(self, capacity: int, per_seconds: float):
        self.capacity = capacity
        self.rate = capacity / per_seconds
        self.tokens = float(capacity)
        self.updated = time.monotonic()

    def take(self) -> Decision:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        allowed = self.tokens >= 1
        if allowed:
            self.tokens -= 1
        reset = math.ceil((self.capacity - self.tokens) / self.rate) if self.tokens < self.capacity else 0
        return Decision(allowed, self.capacity, int(self.tokens), reset)


class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}

    def check(self, key: str, capacity: int, per_seconds: float) -> Decision:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = TokenBucket(capacity, per_seconds)
        return bucket.take()

    def reset(self) -> None:
        self._buckets.clear()
