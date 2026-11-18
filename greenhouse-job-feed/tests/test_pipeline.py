"""maxItems is split fairly across boards (no network: boards are synthetic)."""

import asyncio

import pytest

import src.pipeline as pl
from src.greenhouse import JobFilter, Options
from src.models import BoardRef, GreenhouseError, Job
from src.pipeline import ERRORED, COMPLETED, STOPPED, fair_caps, iter_jobs


def refs_of(*tokens):
    return [BoardRef(t) for t in tokens]


def make_job(token, i):
    return Job(token, i, {"jobId": i, "boardToken": token})


@pytest.fixture
def source(monkeypatch):
    """``spec[token] = n`` jobs, ``"boom"`` (raises at once), ``("fail_after", n)`` or
    ``("wait", event, n)`` (blocks until the event is set). Unlisted boards have 1000 jobs."""
    spec: dict = {}

    async def fake(client, ref, flt, opts, **kw):
        what = spec.get(ref.token, 1000)
        n = what
        if what == "boom":
            raise GreenhouseError("kaput")
        if isinstance(what, tuple) and what[0] == "wait":
            await what[1].wait()
            n = what[2]
        if isinstance(what, tuple) and what[0] == "fail_after":
            n = what[1]
        for i in range(n):
            yield make_job(ref.token, i)
            await asyncio.sleep(0)
        if isinstance(what, tuple) and what[0] == "fail_after":
            raise GreenhouseError("late failure")

    monkeypatch.setattr(pl, "fetch_board", fake)
    return spec


async def collect(refs, **kw):
    return [j async for j in iter_jobs(None, refs, JobFilter(), Options(), **kw)]


def tally(jobs):
    t: dict[str, int] = {}
    for j in jobs:
        t[j.board_token] = t.get(j.board_token, 0) + 1
    return t


# ---------- fair_caps ----------
def test_fair_caps_even_split_and_remainder_is_first_come():
    assert fair_caps([0, 0, 0], [False] * 3, 9) == [3, 3, 3]
    assert fair_caps([0, 0, 0], [False] * 3, 10) == [4, 4, 4]
    assert fair_caps([4, 0, 0], [False] * 3, 10) == [4, 3, 3]


def test_fair_caps_leftover_of_finished_board_goes_to_the_rest():
    assert fair_caps([1, 0, 0], [True, False, False], 10) == [1, 5, 5]
    assert fair_caps([0, 0, 0, 0], [True, True, False, False], 8) == [0, 0, 4, 4]


def test_fair_caps_board_over_its_share_is_frozen_not_shrunk():
    assert fair_caps([8, 1, 1], [False] * 3, 12) == [8, 2, 2]


# ---------- pipeline ----------
async def test_every_board_gets_a_share(source):
    refs = refs_of(*"abcdefghij")
    assert tally(await collect(refs, max_items=10)) == {c: 1 for c in "abcdefghij"}
    t = tally(await collect(refs, max_items=25))
    assert sum(t.values()) == 25 and set(t.values()) <= {2, 3} and len(t) == 10


async def test_max_items_below_board_count_still_delivers_max_items(source):
    t = tally(await collect(refs_of(*"abcde"), max_items=3))
    assert sum(t.values()) == 3 and max(t.values()) == 1


async def test_leftover_of_short_boards_is_redistributed(source):
    source.update(a=2, b=3)
    t = tally(await collect(refs_of("a", "b", "c", "d"), max_items=20))
    assert t["a"] == 2 and t["b"] == 3
    assert sorted((t["c"], t["d"])) == [7, 8] and sum(t.values()) == 20


async def test_leftover_of_empty_and_failing_boards_is_redistributed(source):
    source.update(empty=0, boom="boom", late=("fail_after", 1))
    errors = []
    t = tally(await collect(refs_of("empty", "boom", "late", "big1", "big2"), max_items=21,
                            on_error=lambda ref, exc: errors.append(ref.token)))
    assert "empty" not in t and "boom" not in t and t["late"] == 1
    assert t["big1"] + t["big2"] == 20 and abs(t["big1"] - t["big2"]) <= 1
    assert sorted(errors) == ["boom", "late"]


async def test_everything_is_delivered_when_supply_is_below_max_items(source):
    source.update(a=2, b=3)
    assert tally(await collect(refs_of("a", "b"), max_items=100)) == {"a": 2, "b": 3}


async def test_slow_board_does_not_block_others_and_keeps_its_share(source):
    release = asyncio.Event()
    source.update(slow=("wait", release, 100))
    got = []

    async def consume():
        async for j in iter_jobs(None, refs_of("slow", "fast"), JobFilter(), Options(), max_items=10):
            got.append(j.board_token)
            if got.count("fast") == 5:
                release.set()

    await asyncio.wait_for(consume(), 5)
    t = tally([make_job(t, 0) for t in got])
    assert t == {"fast": 5, "slow": 5}   # fast was held at its share until slow arrived


async def test_prior_deliveries_count_toward_the_share(source):
    t = tally(await collect(refs_of("a", "b"), max_items=6, prior=[4, 0]))
    assert t == {"a": 1, "b": 5}   # 4 + 6 = 10 slots overall: 5 each, "a" already holds 4


async def test_seen_keys_are_skipped_and_not_counted(source):
    out = await collect(refs_of("a"), max_items=3, seen={"a|0", "a|1"})
    assert [j.job_id for j in out] == [2, 3, 4]


async def test_outcomes_completed_error_stopped(source):
    source.update(done=2, boom="boom")
    outcomes = {}
    await collect(refs_of("done", "boom", "big"), max_items=10, outcomes=outcomes, on_error=lambda *_: None)
    assert outcomes == {"done": COMPLETED, "boom": ERRORED, "big": STOPPED}


async def test_boards_loading_is_bounded(monkeypatch):
    active = peak = 0

    async def fake(client, ref, flt, opts, board_gate=None, **kw):
        nonlocal active, peak
        async with board_gate:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
        yield make_job(ref.token, 0)

    monkeypatch.setattr(pl, "fetch_board", fake)
    out = await collect(refs_of(*"abcdefgh"), max_items=8, max_parallel_boards=2)
    assert len(out) == 8 and peak == 2
