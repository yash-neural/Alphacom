import time

import pytest

from app import webhooks
from app.pagination import decode_cursor, encode_cursor
from app.problems import ApiProblem
from app.ratelimit import RateLimiter
from app.resilience import CircuitBreaker, CircuitOpenError, UpstreamError, call_upstream

# ---- resilience ----


async def test_retries_then_succeeds():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise UpstreamError("x", "timeout")
        return "ok"

    assert await call_upstream(CircuitBreaker("x"), flaky, attempts=3, base_delay_s=0) == "ok"
    assert calls == 3


async def test_gives_up_after_attempts():
    async def down():
        raise UpstreamError("x", "HTTP 503")

    with pytest.raises(UpstreamError):
        await call_upstream(CircuitBreaker("x"), down, attempts=2, base_delay_s=0)


async def test_breaker_opens_and_fails_fast_then_half_opens():
    now = [0.0]
    breaker = CircuitBreaker("x", failure_threshold=2, reset_after_s=30, clock=lambda: now[0])
    calls = 0

    async def down():
        nonlocal calls
        calls += 1
        raise UpstreamError("x", "HTTP 503")

    for _ in range(2):
        with pytest.raises(UpstreamError):
            await call_upstream(breaker, down, attempts=1)
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        await call_upstream(breaker, down, attempts=1)
    assert calls == 2  # the open circuit did not call upstream

    now[0] += 31
    assert breaker.state == "half_open"

    async def up():
        return 1

    assert await call_upstream(breaker, up, attempts=1) == 1
    assert breaker.state == "closed"


# ---- cursors ----


def test_cursor_roundtrip():
    c = encode_cursor("s", ["2026-11-01T00:00:00Z", "CS-1"], "fp")
    assert decode_cursor("s", c, "fp") == ["2026-11-01T00:00:00Z", "CS-1"]


@pytest.mark.parametrize("mutate", [
    lambda c: c[:-2] + ("AA" if not c.endswith("AA") else "BB"),  # tampered signature
    lambda c: "x" + c,                                             # tampered body
    lambda c: "garbage",
])
def test_cursor_tampering_rejected(mutate):
    c = encode_cursor("s", ["a", "b"], "fp")
    with pytest.raises(ApiProblem) as e:
        decode_cursor("s", mutate(c), "fp")
    assert e.value.status == 400


def test_cursor_bound_to_filters():
    c = encode_cursor("s", ["a", "b"], "fp1")
    with pytest.raises(ApiProblem, match="different filters"):
        decode_cursor("s", c, "fp2")


# ---- rate limit ----


def test_token_bucket():
    rl = RateLimiter()
    results = [rl.check("k", 3, 60).allowed for _ in range(4)]
    assert results == [True, True, True, False]
    assert rl.check("other", 3, 60).allowed  # buckets are per key


# ---- webhook signatures ----


def test_signature_verifies_and_rejects_tampering():
    body = b'{"id":"evt_1"}'
    ts = int(time.time())
    sig = webhooks.sign("whsec_a", "evt_1", ts, body)
    assert webhooks.verify(["whsec_a"], "evt_1", str(ts), sig, body)
    assert not webhooks.verify(["whsec_a"], "evt_1", str(ts), sig, body + b" ")
    assert not webhooks.verify(["whsec_b"], "evt_1", str(ts), sig, body)
    assert not webhooks.verify(["whsec_a"], "evt_2", str(ts), sig, body)


def test_signature_rejects_stale_timestamp():
    body = b"{}"
    old = int(time.time()) - 600
    sig = webhooks.sign("whsec_a", "evt_1", old, body)
    assert not webhooks.verify(["whsec_a"], "evt_1", str(old), sig, body)


def test_signature_accepts_either_secret_during_rotation():
    body, ts = b"{}", int(time.time())
    header = f"{webhooks.sign('old', 'e', ts, body)} {webhooks.sign('new', 'e', ts, body)}"
    assert webhooks.verify(["new"], "e", str(ts), header, body)


def test_ssrf_guard():
    with pytest.raises(ApiProblem):
        webhooks.validate_target_url("http://example.com/hook", (), is_dev=False)  # not https
    with pytest.raises(ApiProblem):
        webhooks.validate_target_url("https://127.0.0.1/hook", (), is_dev=False)   # loopback
    with pytest.raises(ApiProblem):
        webhooks.validate_target_url("https://169.254.169.254/latest", (), is_dev=False)  # metadata
    webhooks.validate_target_url("http://receiver/hooks", ("receiver",), is_dev=True)  # dev allowlist
