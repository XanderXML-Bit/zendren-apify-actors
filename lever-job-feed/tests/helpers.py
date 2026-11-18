"""Shared helpers for the run-level tests."""

from src.main import run
from tests.fakes import FakeActor, FakeLever, Platform, posting


def board(n, prefix="p", **kw):
    """n postings with ids prefix1..prefixN; createdAt decreases with the number (p1 is newest)."""
    return [posting(f"{prefix}{i}", f"Job {i}", created=1_790_000_000_000 - i * 1000, **kw) for i in range(1, n + 1)]


async def go(monkeypatch, boards, inp, platform=None, fail=None):
    """Run the actor against fake Lever boards; returns (platform, lever, actor)."""
    import src.main as m
    lever = FakeLever(boards, fail=fail)
    monkeypatch.setattr(m, "make_client", lever.client)
    p = platform or Platform()
    actor = FakeActor(p, inp)
    await run(actor)
    return p, lever, actor


def ids(p):
    return [r.get("postingId") for r in p.dataset]
