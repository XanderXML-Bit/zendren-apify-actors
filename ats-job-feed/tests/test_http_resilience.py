"""Offline tests: Retry-After handling and the shared per-host cool-down."""

import asyncio
import time

import httpx
import pytest

from src.ats import RateLimitedClient
from src.ats.models import AtsError


class VClock:
    """Virtual time: sleeping advances the clock instantly."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        await _real_sleep(0)


_real_sleep = asyncio.sleep


def vclient(handler, clock, monkeypatch, **kw):
    monkeypatch.setattr("src.ats.http.asyncio.sleep", clock.sleep)
    kw.setdefault("delay", 0)
    return RateLimitedClient(transport=httpx.MockTransport(handler), clock=clock.now,
                             backoff_base=0.01, **kw)


async def test_long_retry_after_is_not_capped_at_60s(monkeypatch):
    clock, n = VClock(), []

    def handler(req):
        n.append(clock.t)
        if len(n) == 1:
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json={"ok": 1})

    async with vclient(handler, clock, monkeypatch) as c:
        assert await c.get_json("https://h.example/x") == {"ok": 1}
    assert n[1] - n[0] >= 120  # was shortened to 60 before
    assert max(clock.sleeps) >= 119.9


async def test_backoff_itself_is_still_capped(monkeypatch):
    clock, n = VClock(), []

    def handler(req):
        n.append(clock.t)
        return httpx.Response(500) if len(n) < 9 else httpx.Response(200, json={})

    async with vclient(handler, clock, monkeypatch, max_retries=10, ) as c:
        c._backoff_base = 100.0  # local backoff would be 100, 200, ...
        await c.get_json("https://h.example/x")
    assert max(clock.sleeps) <= 60.5


async def test_retry_after_beyond_limit_fails_fast(monkeypatch):
    clock, n = VClock(), []

    def handler(req):
        n.append(1)
        return httpx.Response(429, headers={"Retry-After": "7200"})

    async with vclient(handler, clock, monkeypatch, max_retry_after=900) as c:
        with pytest.raises(AtsError, match="Retry-After"):
            await c.get_json("https://h.example/x")
    assert len(n) == 1 and clock.sleeps == []


async def test_http_date_retry_after(monkeypatch):
    clock, n = VClock(), []
    import email.utils

    def handler(req):
        n.append(clock.t)
        if len(n) == 1:
            return httpx.Response(503, headers={"Retry-After": email.utils.formatdate(time.time() + 30)})
        return httpx.Response(200, json={})

    async with vclient(handler, clock, monkeypatch) as c:
        await c.get_json("https://h.example/x")
    assert n[1] - n[0] >= 28


async def test_throttle_cools_down_whole_host_not_just_one_request():
    """After one request gets a 429 + Retry-After, nobody else may hit the host until it expires."""
    starts, state = [], {"throttled": False}
    t0 = time.monotonic()

    def handler(req):
        starts.append(time.monotonic() - t0)
        if not state["throttled"]:
            state["throttled"] = True
            return httpx.Response(429, headers={"Retry-After": "0.4"})
        return httpx.Response(200, json={})

    async with RateLimitedClient(transport=httpx.MockTransport(handler), delay=0,
                                 per_host_concurrency=4, backoff_base=0.01) as c:
        first = asyncio.create_task(c.get_json("https://h.example/0"))
        await asyncio.sleep(0.05)  # the 429 has been received
        others = [asyncio.create_task(c.get_json(f"https://h.example/{i}")) for i in range(1, 6)]
        await asyncio.gather(first, *others)
    assert starts[0] < 0.1
    assert min(starts[1:]) >= 0.38, starts  # all follow-ups waited out the Retry-After


async def test_other_hosts_are_not_cooled_down():
    done = {}
    t0 = time.monotonic()

    def handler(req):
        if req.url.host == "slow.example" and "slow" not in done:
            done["slow"] = True
            return httpx.Response(429, headers={"Retry-After": "0.5"})
        done[req.url.host + req.url.path] = time.monotonic() - t0
        return httpx.Response(200, json={})

    async with RateLimitedClient(transport=httpx.MockTransport(handler), delay=0) as c:
        slow = asyncio.create_task(c.get_json("https://slow.example/a"))
        await asyncio.sleep(0.05)
        await c.get_json("https://fast.example/b")
        assert done["fast.example/b"] < 0.3
        await slow


async def test_requests_to_a_host_are_evenly_spaced():
    starts = []

    def handler(req):
        starts.append(time.monotonic())
        return httpx.Response(200, json={})

    async with RateLimitedClient(transport=httpx.MockTransport(handler), delay=0.05,
                                 per_host_concurrency=4) as c:
        await asyncio.gather(*(c.get_json(f"https://h.example/{i}") for i in range(5)))
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 0.04 for g in gaps), gaps  # no burst of 4 simultaneous requests


# ---------- review 2: M1 / M2 ----------
async def test_final_throttled_response_still_cools_down_host(monkeypatch):
    """The last attempt's 429 + Retry-After must delay the next request to the host (M1)."""
    clock, n = VClock(), []
    asks = iter(["120", "300"])

    def handler(req):
        n.append(clock.t)
        return httpx.Response(429, headers={"Retry-After": next(asks, "300")})

    async with vclient(handler, clock, monkeypatch, max_retries=1) as c:
        with pytest.raises(AtsError, match="giving up"):
            await c.get_json("https://h.example/a")
        assert n == [1000.0, 1120.0]  # retry waited for the first Retry-After
        with pytest.raises(AtsError):
            await c.get_json("https://h.example/b")
    assert n[2] >= 1120 + 300  # second request honoured the *final* response's 300s


async def test_cooldown_applies_even_without_retries(monkeypatch):
    clock, n = VClock(), []

    def handler(req):
        n.append(clock.t)
        return httpx.Response(429, headers={"Retry-After": "50"})

    async with vclient(handler, clock, monkeypatch, max_retries=0) as c:
        with pytest.raises(AtsError):
            await c.get_json("https://h.example/a")
        with pytest.raises(AtsError):
            await c.get_json("https://h.example/b")
    assert n[1] - n[0] >= 50


async def test_oversized_retry_after_makes_siblings_fail_fast(monkeypatch):
    """After a 7200s ask (limit 900) other requests to that host must not sleep for it (M2)."""
    clock, n = VClock(), []

    def handler(req):
        n.append(clock.t)
        return httpx.Response(429, headers={"Retry-After": "7200"})

    async with vclient(handler, clock, monkeypatch, max_retry_after=900) as c:
        with pytest.raises(AtsError, match="Retry-After"):
            await c.get_json("https://h.example/a")
        with pytest.raises(AtsError, match="cooling down"):
            await c.get_json("https://h.example/b")
        # a different host is unaffected
        with pytest.raises(AtsError, match="Retry-After"):
            await c.get_json("https://other.example/x")
    assert len(n) == 2 and clock.sleeps == []  # nothing was sent to h.example again, nothing slept


async def test_waiter_queued_before_oversized_ask_fails_fast(monkeypatch):
    clock, n = VClock(), []

    def handler(req):
        n.append(req.url.path)
        return httpx.Response(429, headers={"Retry-After": "7200"})

    async with vclient(handler, clock, monkeypatch, max_retry_after=900, per_host_concurrency=1,
                       delay=5) as c:
        results = await asyncio.gather(*(c.get_json(f"https://h.example/{i}") for i in range(3)),
                                       return_exceptions=True)
    assert all(isinstance(r, AtsError) for r in results)
    assert len(n) == 1  # only the first request was ever sent
    assert max(clock.sleeps, default=0) <= 900
