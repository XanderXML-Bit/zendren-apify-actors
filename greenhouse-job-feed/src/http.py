"""Polite async HTTP client: per-host concurrency cap, small delay, retries."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from datetime import timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from .models import GreenhouseError, HttpStatusError

log = logging.getLogger("ghfeed.http")

USER_AGENT = (
    "GreenhouseJobFeed/0.1 (+https://apify.com; reads the public Greenhouse Job Board "
    "API; low-rate, honours Retry-After)"
)
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Cap for our own exponential backoff only. A server-sent Retry-After is honoured
# in full (up to ``max_retry_after``), never shortened to this value.
MAX_BACKOFF = 60.0
# Longest Retry-After we are willing to wait for; a larger request fails the call.
MAX_RETRY_AFTER = 900.0
# Statuses that put the whole host into cool-down (others only back off locally).
COOLDOWN_STATUSES = {429, 503}


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:  # "-0000" style dates parse as naive; HTTP dates are UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, dt.timestamp() - (now if now is not None else time.time()))


class _Host:
    """Per-host state shared by every request to that host."""

    def __init__(self, concurrency: int) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.lock = asyncio.Lock()
        self.next_allowed = 0.0  # spacing: next free start slot (clock() time)
        self.cooldown_until = 0.0  # throttling: no request may start before this


class RateLimitedClient:
    """Per-host concurrency cap, evenly spaced requests, shared cool-down, retries.

    Every request to a host reserves the next free start slot (``delay`` apart), so
    the host sees one evenly spaced stream whatever the concurrency. A 429/503 (or any
    retryable response with ``Retry-After``) pushes the host's next-allowed time
    forward, which delays *all* queued requests to that host, not just the one that
    was throttled.
    """

    def __init__(
        self,
        *,
        per_host_concurrency: int = 4,
        delay: float = 0.15,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        timeout: float = 45.0,
        max_retry_after: float = MAX_RETRY_AFTER,
        user_agent: str = USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_host = per_host_concurrency
        self._delay = delay
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._max_retry_after = max_retry_after
        self._clock = clock
        self._hosts: dict[str, _Host] = {}
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> "RateLimitedClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _host(self, url: str) -> _Host:
        name = urlsplit(url).netloc.lower()
        if name not in self._hosts:
            self._hosts[name] = _Host(self._per_host)
        return self._hosts[name]

    async def _wait_for_slot(self, host: _Host) -> None:
        """Reserve the next start slot on this host and sleep until it arrives.

        A cool-down registered while we slept (another request got throttled)
        invalidates the slot, so we reserve again.
        """
        while True:
            async with host.lock:
                now = self._clock()
                if host.cooldown_until - now > self._max_retry_after:
                    # the host asked for more than we are willing to wait: fail fast
                    # instead of sleeping for hours (the host stays blocked meanwhile)
                    raise GreenhouseError(
                        f"host cooling down for {host.cooldown_until - now:.0f}s "
                        f"(Retry-After), more than the {self._max_retry_after:.0f}s limit")
                start = max(now, host.next_allowed, host.cooldown_until)
                host.next_allowed = start + self._delay
            if start > now:
                await asyncio.sleep(start - now)
            if start >= host.cooldown_until:
                return

    def _cool_down(self, host: _Host, seconds: float) -> None:
        host.cooldown_until = max(host.cooldown_until, self._clock() + seconds)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = self._host(url)
        last: str = ""
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            cool = False
            async with host.sem:
                await self._wait_for_slot(host)
                try:
                    resp = await self._client.request(method, url, **kwargs)
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last = f"{type(exc).__name__}: {exc}"
                    resp = None
            if resp is not None:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in RETRY_STATUSES:
                    raise HttpStatusError(resp.status_code, url)
                last = f"HTTP {resp.status_code}"
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                cool = resp.status_code in COOLDOWN_STATUSES or retry_after is not None
            if retry_after is not None and retry_after > self._max_retry_after:
                self._cool_down(host, retry_after)
                raise GreenhouseError(
                    f"{method} {url}: server asked to wait {retry_after:.0f}s "
                    f"(Retry-After), more than the {self._max_retry_after:.0f}s limit"
                )
            backoff = min(
                MAX_BACKOFF,
                self._backoff_base * (2**attempt) + random.uniform(0, 0.25),
            )
            wait = max(retry_after or 0.0, backoff)
            if cool and attempt == self._max_retries:
                # last attempt: no retry, but the throttle still applies to the host
                self._cool_down(host, wait)
            if attempt == self._max_retries:
                break
            log.warning("%s %s failed (%s); retry %d/%d in %.1fs",
                        method, url, last, attempt + 1, self._max_retries, wait)
            if cool:
                # shared cool-down: the next attempt (and every other request to
                # this host) waits in _wait_for_slot, outside the semaphore
                self._cool_down(host, wait)
            else:
                await asyncio.sleep(wait)  # semaphore is not held while waiting
        raise GreenhouseError(f"giving up on {method} {url}: {last}")

    async def get_json(self, url: str, params: dict | None = None) -> Any:
        return self._decode(await self.request("GET", url, params=params), url)

    async def post_json(self, url: str, payload: dict) -> Any:
        return self._decode(await self.request("POST", url, json=payload), url)

    @staticmethod
    def _decode(resp: httpx.Response, url: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise GreenhouseError(f"invalid JSON from {url}") from exc
