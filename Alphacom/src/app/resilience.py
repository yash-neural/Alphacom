"""Retry with backoff + circuit breaker for upstream calls.

Kept small on purpose; in production I'd reach for the platform's standard
library (resilience4j / Polly / a service mesh policy) rather than own this.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.logs import log

logger = logging.getLogger("app.resilience")
T = TypeVar("T")


class UpstreamError(Exception):
    """Upstream failed in a way worth retrying (timeout, connection error, 5xx)."""

    def __init__(self, system: str, reason: str):
        super().__init__(f"{system}: {reason}")
        self.system = system
        self.reason = reason


class CircuitOpenError(UpstreamError):
    pass


class CircuitBreaker:
    """Closed -> open after N consecutive failures; half-open after a cool-down lets
    one trial call through; success closes it, failure re-opens it."""

    def __init__(self, name: str, failure_threshold: int = 5, reset_after_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.name = name
        self.clock = clock
        self.failure_threshold = failure_threshold
        self.reset_after_s = reset_after_s
        self.failures = 0
        self.opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.reset_after_s:
            return "half_open"
        return "open"

    def before_call(self) -> None:
        state = self.state
        if state == "open" or (state == "half_open" and self._trial_in_flight):
            raise CircuitOpenError(self.name, "circuit open")
        if state == "half_open":
            self._trial_in_flight = True

    def on_success(self) -> None:
        if self.opened_at is not None:
            log(logger, logging.INFO, "circuit closed", upstream=self.name)
        self.failures = 0
        self.opened_at = None
        self._trial_in_flight = False

    def on_failure(self) -> None:
        self._trial_in_flight = False
        self.failures += 1
        if self.opened_at is not None or self.failures >= self.failure_threshold:
            self.opened_at = self.clock()
            log(logger, logging.WARNING, "circuit opened", upstream=self.name, failures=self.failures)


async def call_upstream(
    breaker: CircuitBreaker,
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay_s: float = 0.1,
) -> T:
    """Run fn with the breaker; retry UpstreamError with exponential backoff + full jitter.

    Only call this with retry attempts > 1 for operations that are safe to repeat.
    """
    last: UpstreamError | None = None
    for attempt in range(1, attempts + 1):
        breaker.before_call()
        try:
            result = await fn()
        except UpstreamError as e:
            breaker.on_failure()
            last = e
            log(logger, logging.WARNING, "upstream call failed",
                upstream=breaker.name, attempt=attempt, reason=e.reason)
            if attempt < attempts and breaker.state == "closed":
                await asyncio.sleep(random.uniform(0, base_delay_s * 2 ** (attempt - 1)))
                continue
            break
        breaker.on_success()
        return result
    assert last is not None
    raise last
