"""Regression tests for review findings: record keys, state cap, run lock,
unconfirmed charges vs. the named state, stable identity, discoverFilters budget."""

import re
from decimal import Decimal as D

import pytest

import src.seen as seen_mod
from src.seen import (
    LOCK_PREFIX, MAX_KEY_LENGTH, STORE_NAME, SeenStore, StateLock, StateLockedError, lock_key, record_key,
    today_days,
)
from tests.fakes import Platform, posting
from tests.helpers import board, go, ids

START, JOB = D("0.00005"), D("0.0015")
PRICES = {"apify-actor-start": START, "job": JOB}
INP = {"sites": ["acme"], "onlyNewJobs": True, "maxItems": 100, "descriptionFormat": "none"}


def S(p):
    return p.kv["SUMMARY"]


def plat(max_total="10", **kw):
    p = Platform(prices=dict(PRICES), max_total=D(max_total), **kw)
    p.charged["apify-actor-start"] = 1
    return p


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)
    monkeypatch.delenv("ACTOR_RUN_ID", raising=False)


# ---------------------------------------------------------------- record keys
def test_record_key_shape_is_always_prefix_and_hash():
    for original in ("daily", "a b", "a_b", "x" * 400, "ünï/cödé", "default"):
        assert re.fullmatch(r"STATE-[A-Za-z0-9!\-_.'()]*-[0-9a-f]{12}", record_key(original)), original


def test_punctuation_collisions_do_not_collide():
    keys = [record_key(k) for k in ("daily/a", "daily?a", "daily_a", "daily a", "daily*a")]
    assert len(set(keys)) == len(keys)
    prefixes = {k.rsplit("-", 1)[0] for k in keys}
    assert prefixes == {"STATE-daily_a"}  # same readable prefix, different hash


def test_valid_looking_key_cannot_equal_another_keys_sanitized_form():
    # "a b" sanitizes to "a_b"; a user key that looks like "a_b-<that hash>" must not map to the same record
    first = record_key("a b")
    assert first.startswith("STATE-a_b-")
    imitation = first.removeprefix("STATE-")
    assert record_key(imitation) != first
    assert record_key("a b") != record_key("a_b")


def test_unicode_keys_differ_and_stay_ascii():
    a, b = record_key("日本語"), record_key("中文字")
    assert a != b and a.isascii() and b.isascii()
    assert a.startswith("STATE-___-") and b.startswith("STATE-___-")


def test_long_prefix_keeps_hash_and_fits_with_lock_prefix():
    a, b = record_key("x" * 300 + "A"), record_key("x" * 300 + "B")
    assert a != b
    assert len(a) <= MAX_KEY_LENGTH - len(LOCK_PREFIX) and len(lock_key("x" * 300)) <= MAX_KEY_LENGTH
    assert re.search(r"-[0-9a-f]{12}$", a)


def test_hash_is_of_the_original_key_and_stable():
    import hashlib
    assert record_key("daily/a").endswith("-" + hashlib.sha256(b"daily/a").hexdigest()[:12])
    assert record_key("") == record_key("default") == record_key("   ")


# ---------------------------------------------------------------- state cap
class KV:
    def __init__(self, data=None):
        self.d = data or {}

    async def get_value(self, k, default_value=None):
        return self.d.get(k, default_value)

    async def set_value(self, k, v):
        self.d[k] = v

    async def delete_value(self, k):
        self.d.pop(k, None)


async def test_cap_enforced_on_load_least_recently_seen_first_stable_ties():
    today = today_days()
    kv = KV({record_key("k"): {"seen": {"a": today - 5, "b": today - 1, "c": today - 5, "d": today - 5, "e": today}}})
    s = SeenStore(kv, "k", today=today, max_entries=3)
    await s.load()
    # oldest day first; the three day-5 keys tie, stored order decides: a and c go
    assert set(s.seen) == {"d", "b", "e"} and s.evicted == 2


async def test_cap_enforced_on_save_after_age_pruning():
    today = today_days()
    kv = KV()
    s = SeenStore(kv, "k", today=today, max_entries=3)
    await s.load()
    s.seen = {"old": today - 200, "x": today - 3, "y": today - 2, "z": today - 1}
    s.mark(["new"])
    await s.save()
    saved = kv.d[record_key("k")]["seen"]
    assert set(saved) == {"y", "z", "new"}   # "old" aged out, then "x" (oldest of the rest) was evicted
    assert s.pruned == 1 and s.evicted == 1


async def test_over_cap_state_drops_expired_keys_before_valid_ones_on_load():
    today = today_days()
    kv = KV({record_key("k"): {"seen": {"exp": today - 500, "a": today - 3, "b": today - 2, "c": today - 1}}})
    s = SeenStore(kv, "k", today=today, max_entries=3)
    await s.load()
    assert set(s.seen) == {"a", "b", "c"} and s.pruned == 1 and s.evicted == 0


def test_default_cap_value():
    assert seen_mod.MAX_ENTRIES == 200_000


async def test_run_reports_state_evicted(monkeypatch):
    monkeypatch.setattr(seen_mod, "MAX_ENTRIES", 4)
    today = today_days()
    named = {STORE_NAME: {record_key("default"): {"version": 1, "seen": {
        f"other|{i}": today - 10 - i for i in range(6)}}}}
    p, *_ = await go(monkeypatch, {("global", "acme"): board(3)}, INP, platform=Platform(named=named))
    s = S(p)
    assert s["stateEvicted"] >= 2 and ids(p) == ["p1", "p2", "p3"]
    assert len(p.named[STORE_NAME][record_key("default")]["seen"]) <= 4
    assert s["incremental"]["tracked"] <= 4


async def test_state_evicted_is_null_without_incremental_mode(monkeypatch):
    p, *_ = await go(monkeypatch, {("global", "acme"): board(1)}, {"sites": ["acme"]})
    assert S(p)["stateEvicted"] is None


# ---------------------------------------------------------------- run lock
def named_with_lock(run_id, state_key="default"):
    return {STORE_NAME: {lock_key(state_key): {"runId": run_id, "at": "2026-10-19T10:00:00Z"}}}


@pytest.mark.parametrize("status", ["RUNNING", "READY"])
async def test_active_run_with_same_state_key_stops_this_run(monkeypatch, status):
    monkeypatch.setenv("ACTOR_RUN_ID", "me")
    p = Platform(named=named_with_lock("other"), runs={"other": status})
    _, lever, a = await go(monkeypatch, {("global", "acme"): board(3)}, INP, platform=p)
    assert a.failed and "other" in a.failed and "stateKey" in a.failed
    assert lever.requests == [] and p.dataset == []
    assert p.named[STORE_NAME][lock_key("default")]["runId"] == "other"  # the holder keeps its lock


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"])
async def test_finished_holder_is_taken_over_and_lock_released(monkeypatch, status):
    monkeypatch.setenv("ACTOR_RUN_ID", "me")
    p = Platform(named=named_with_lock("other"), runs={"other": status})
    p, _, a = await go(monkeypatch, {("global", "acme"): board(3)}, INP, platform=p)
    assert not a.failed and len(p.dataset) == 3
    assert lock_key("default") not in p.named[STORE_NAME]  # released at the end


async def test_unknown_or_unreadable_holder_does_not_block(monkeypatch):
    monkeypatch.setenv("ACTOR_RUN_ID", "me")
    p = Platform(named=named_with_lock("ghost"))
    p, _, a = await go(monkeypatch, {("global", "acme"): board(2)}, INP, platform=p)
    assert not a.failed and len(p.dataset) == 2
    p2 = Platform(named=named_with_lock("other"), runs={"other": "RUNNING"}, run_lookup_error=True)
    p2, _, a2 = await go(monkeypatch, {("global", "acme"): board(2)}, INP, platform=p2)
    assert not a2.failed and len(p2.dataset) == 2


async def test_own_lock_after_restart_is_taken_back(monkeypatch):
    monkeypatch.setenv("ACTOR_RUN_ID", "me")
    p = Platform(named=named_with_lock("me"), runs={"me": "RUNNING"})
    _, _, a = await go(monkeypatch, {("global", "acme"): board(2)}, INP, platform=p)
    assert not a.failed and len(p.dataset) == 2


async def test_lock_is_held_during_the_run_and_other_state_keys_are_independent(monkeypatch):
    monkeypatch.setenv("ACTOR_RUN_ID", "me")
    p = Platform(named=named_with_lock("other", "weekly"), runs={"other": "RUNNING"})
    seen_lock = {}
    import src.main as m
    real = m.fetch_all

    async def spy(*a, **kw):
        seen_lock.update(p.named[STORE_NAME].get(lock_key("daily"), {}))
        return await real(*a, **kw)

    monkeypatch.setattr(m, "fetch_all", spy)
    p, _, a = await go(monkeypatch, {("global", "acme"): board(2)}, {**INP, "stateKey": "daily"}, platform=p)
    assert not a.failed and seen_lock["runId"] == "me" and seen_lock["at"].endswith("Z")
    assert lock_key("weekly") in p.named[STORE_NAME]  # someone else's lock is untouched


async def test_lock_released_when_the_run_crashes(monkeypatch):
    monkeypatch.setenv("ACTOR_RUN_ID", "me")
    p = Platform(crash_on_push=2)
    with pytest.raises(RuntimeError):
        await go(monkeypatch, {("global", "acme"): board(5)}, INP, platform=p)
    assert lock_key("default") not in p.named[STORE_NAME]


async def test_lock_skipped_without_run_id_or_incremental_mode(monkeypatch):
    p, *_ = await go(monkeypatch, {("global", "acme"): board(2)}, INP)  # no ACTOR_RUN_ID
    assert not any(k.startswith("LOCK-") for k in p.named[STORE_NAME])
    monkeypatch.setenv("ACTOR_RUN_ID", "me")
    p, *_ = await go(monkeypatch, {("global", "acme"): board(2)}, {"sites": ["acme"]})
    assert p.named == {}


async def test_simultaneous_start_loser_stops():
    kv = KV()
    p = Platform(runs={"first": "RUNNING"})
    from tests.fakes import _ApifyClient
    client = _ApifyClient(p)
    lock = StateLock(kv, "k", "second", client)

    real_set = kv.set_value

    async def racing_set(key, value):
        await real_set(key, value)
        kv.d[key] = {"runId": "first", "at": "now"}  # the other run wrote right after us

    kv.set_value = racing_set
    with pytest.raises(StateLockedError):
        await lock.acquire()
    await lock.release()
    assert kv.d[lock_key("k")]["runId"] == "first"


# ---------------------------------------------------------------- unconfirmed charges vs named state
async def test_crash_restart_only_new_keeps_unconfirmed_row_out_of_state(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    boards = {("global", "acme"): board(60)}
    p = plat(crash_after_write_on_push=6)  # 6th row written, its charge never sent
    with pytest.raises(RuntimeError):
        await go(monkeypatch, boards, {**INP, "maxItems": 10}, platform=p)
    p.crash_after_write_on_push = None
    await go(monkeypatch, boards, {**INP, "maxItems": 10}, platform=p)
    assert len(ids(p)) == 10 and p.charged["job"] == 9 and list(S(p)["unbilledJobs"]) == ["acme|p6"]
    state = p.named[STORE_NAME][record_key("default")]["seen"]
    assert "acme|p6" not in state                      # unconfirmed: never remembered
    assert {f"acme|p{i}" for i in (1, 2, 3, 4, 5, 7, 8, 9, 10)} <= set(state)
    # the next scheduled run (fresh storage, same named state) delivers it again, now billed
    p2 = plat(named=p.named)
    await go(monkeypatch, boards, {**INP, "maxItems": 3}, platform=p2)
    assert ids(p2) == ["p6", "p11", "p12"] and p2.charged["job"] == 3
    assert "acme|p6" in p.named[STORE_NAME][record_key("default")]["seen"]


async def test_unconfirmed_row_stays_out_of_state_over_a_second_restart(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    boards = {("global", "acme"): board(60)}
    p = plat(crash_after_write_on_push=3)
    with pytest.raises(RuntimeError):
        await go(monkeypatch, boards, {**INP, "maxItems": 8}, platform=p)
    p.crash_after_write_on_push = None
    p.crash_on_push = 3  # dies again right after restarting
    with pytest.raises(RuntimeError):
        await go(monkeypatch, boards, {**INP, "maxItems": 8}, platform=p)
    p.crash_on_push = None
    await go(monkeypatch, boards, {**INP, "maxItems": 8}, platform=p)
    assert "acme|p3" not in p.named[STORE_NAME][record_key("default")]["seen"]
    assert list(S(p)["unbilledJobs"]) == ["acme|p3"] and len(ids(p)) == 8


# ---------------------------------------------------------------- stable identity
async def test_identity_key_is_independent_of_region_route_and_output_options(monkeypatch):
    eu = {("eu", "pnl"): board(3, "e")}
    named = {}
    p1, *_ = await go(monkeypatch, eu, {"sites": ["pnl"], "onlyNewJobs": True, "descriptionFormat": "none",
                                        "includeLists": False}, platform=Platform(named=named))
    assert p1.dataset[0]["region"] == "eu"  # found through the EU fallback
    keys1 = set(named[STORE_NAME][record_key("default")]["seen"])
    assert keys1 == {"pnl|e1", "pnl|e2", "pnl|e3"}
    p2, *_ = await go(monkeypatch, eu, {"sites": ["https://jobs.eu.lever.co/pnl"], "onlyNewJobs": True,
                                        "descriptionFormat": "markdown", "includeLists": True,
                                        "region": "eu"}, platform=Platform(named=named))
    assert p2.dataset == [] and S(p2)["sites"]["pnl"]["alreadySeen"] == 3
    assert set(named[STORE_NAME][record_key("default")]["seen"]) == keys1


async def test_restart_recovery_key_matches_across_options(monkeypatch):
    """The dataset rows rebuilt after a crash map to the same keys whatever the output options were."""
    boards = {("eu", "pnl"): board(6, "e")}
    p = Platform(crash_on_push=4)
    inp = {"sites": ["pnl"], "descriptionFormat": "html", "includeLists": False, "maxItems": 6}
    with pytest.raises(RuntimeError):
        await go(monkeypatch, boards, inp, platform=p)
    await go(monkeypatch, boards, {**inp, "sites": ["https://jobs.eu.lever.co/pnl"], "region": "eu"}, platform=p)
    assert ids(p) == [f"e{i}" for i in range(1, 7)]


# ---------------------------------------------------------------- discoverFilters charging
async def test_discover_charge_is_budget_safe(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    boards = {("global", s): board(5, s) for s in "abc"}
    p = plat(max_total="0.00305")  # start + 2 job events
    await go(monkeypatch, boards, {"sites": ["a", "b", "c"], "discoverFilters": True}, platform=p)
    assert len(p.dataset) == 2 and p.charged["job"] == 2 and p.spent() <= p.max_total
    assert S(p)["stoppedReason"] == "budget"


async def test_discover_charges_one_job_per_site_record_even_for_big_boards(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = plat()
    await go(monkeypatch, {("global", "a"): board(250, "a")}, {"sites": ["a"], "discoverFilters": True}, platform=p)
    assert len(p.dataset) == 1 and p.charged["job"] == 1 and p.charge_calls == [("job", 1)]
