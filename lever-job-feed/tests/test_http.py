"""HTTP client: retries, Retry-After, 404 not retried (offline)."""

import httpx
import pytest

from src.http import HttpStatusError, LeverError, RateLimitedClient


def client(handler, **kw):
    return RateLimitedClient(transport=httpx.MockTransport(handler), delay=0, backoff_base=0.001, **kw)


async def test_retries_server_errors_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503 if len(calls) < 3 else 200, json=[{"id": "a"}])

    async with client(handler) as c:
        assert await c.get_json("https://api.lever.co/v0/postings/x") == [{"id": "a"}]
    assert len(calls) == 3


async def test_404_is_not_retried_and_carries_status():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404, json={"ok": False, "error": "Document not found"})

    async with client(handler) as c:
        with pytest.raises(HttpStatusError) as ei:
            await c.get_json("https://api.lever.co/v0/postings/nope")
    assert ei.value.status == 404 and len(calls) == 1


async def test_gives_up_after_max_retries_and_invalid_json_is_an_error():
    async with client(lambda r: httpx.Response(500), max_retries=2) as c:
        with pytest.raises(LeverError, match="giving up"):
            await c.get_json("https://api.lever.co/v0/postings/x")
    async with client(lambda r: httpx.Response(200, text="<html>")) as c:
        with pytest.raises(LeverError, match="invalid JSON"):
            await c.get_json("https://api.lever.co/v0/postings/x")


async def test_huge_retry_after_fails_fast():
    async with client(lambda r: httpx.Response(429, headers={"Retry-After": "99999"})) as c:
        with pytest.raises(LeverError, match="Retry-After"):
            await c.get_json("https://api.lever.co/v0/postings/x")
