"""onlyNewJobs / stateKey: named key-value store, first run, repeat runs, leftovers, pruning, restarts."""

import re

import pytest

from src.seen import PRUNE_AFTER_DAYS, STORE_NAME, SeenStore, record_key, today_days
from tests.fakes import Platform, posting
from tests.helpers import board, go, ids


@pytest.fixture(autouse=True)
def no_charging(monkeypatch):
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)


def S(p):
    return p.kv["SUMMARY"]


def store(p, key="default"):
    return p.named[STORE_NAME][record_key(key)]


INP = {"sites": ["acme"], "onlyNewJobs": True, "maxItems": 100}


async def test_first_run_emits_all_second_run_emits_none_then_only_new(monkeypatch):
    boards = {("global", "acme"): board(4)}
    p1, *_ = await go(monkeypatch, boards, INP)
    assert ids(p1) == ["p1", "p2", "p3", "p4"]
    inc = S(p1)["incremental"]
    assert inc["enabled"] and inc["firstRun"] and inc["newRecordsThisRun"] == 4 and inc["tracked"] == 4
    assert inc["store"] == "lever-job-feed-state" and inc["stateKey"] == "default"
    assert set(store(p1)["seen"]) == {"acme|p1", "acme|p2", "acme|p3", "acme|p4"}

    # the next scheduled run is a new Actor run: new default storage, same named store
    p2 = Platform(named=p1.named)
    p2, *_ = await go(monkeypatch, boards, INP, platform=p2)
    assert p2.dataset == [] and S(p2)["incremental"]["firstRun"] is False
    assert S(p2)["incremental"]["alreadySeenSkipped"] == 4
    assert S(p2)["sites"]["acme"]["reason"] == "no_matches" and S(p2)["sites"]["acme"]["alreadySeen"] == 4

    boards[("global", "acme")] = board(4) + [posting("fresh", "Brand new")]
    p3 = Platform(named=p1.named)
    p3, *_ = await go(monkeypatch, boards, INP, platform=p3)
    assert ids(p3) == ["fresh"] and S(p3)["incremental"]["newRecordsThisRun"] == 1
    assert len(store(p3)["seen"]) == 5


async def test_without_only_new_jobs_nothing_is_remembered(monkeypatch):
    p, *_ = await go(monkeypatch, {("global", "acme"): board(2)}, {"sites": ["acme"]})
    assert p.named == {} and S(p)["incremental"] == {"enabled": False}


async def test_max_items_leftovers_are_delivered_by_the_next_run(monkeypatch):
    boards = {("global", "acme"): board(7)}
    named = {}
    got = []
    for expected_reason in ("cap_reached", "cap_reached", "completed"):
        p, *_ = await go(monkeypatch, boards, {**INP, "maxItems": 3}, platform=Platform(named=named))
        got += ids(p)
        assert S(p)["sites"]["acme"]["reason"] == expected_reason
    assert got == ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]  # nothing twice, nothing lost
    p, *_ = await go(monkeypatch, boards, {**INP, "maxItems": 3}, platform=Platform(named=named))
    assert p.dataset == []


async def test_state_keys_are_independent_and_sites_do_not_collide(monkeypatch):
    boards = {("global", "a"): board(2), ("global", "b"): board(2)}  # same posting ids on two sites
    named = {}
    p, *_ = await go(monkeypatch, boards, {**INP, "sites": ["a"], "stateKey": "team-a"}, platform=Platform(named=named))
    assert len(p.dataset) == 2
    p, *_ = await go(monkeypatch, boards, {**INP, "sites": ["a", "b"], "stateKey": "team-a"}, platform=Platform(named=named))
    assert [(r["site"], r["postingId"]) for r in p.dataset] == [("b", "p1"), ("b", "p2")]
    p, *_ = await go(monkeypatch, boards, {**INP, "sites": ["a"], "stateKey": "other"}, platform=Platform(named=named))
    assert len(p.dataset) == 2  # a different stateKey starts from scratch
    assert {record_key("team-a"), record_key("other")} <= set(named[STORE_NAME])


async def test_state_key_is_sanitised_for_the_key_value_store():
    key = record_key("my key/with*odd?chars")
    assert re.fullmatch(r"STATE-my_key_with_odd_chars-[0-9a-f]{12}", key)
    assert record_key("") == record_key(None) == record_key("   ") == record_key("default")
    assert re.fullmatch(r"STATE-default-[0-9a-f]{12}", record_key(""))


async def test_failed_site_is_not_marked_and_retried_next_run(monkeypatch):
    boards = {("global", "a"): board(2), ("global", "b"): board(2)}
    named = {}
    p, *_ = await go(monkeypatch, boards, {**INP, "sites": ["a", "b"]}, platform=Platform(named=named),
                     fail={("global", "b"): 400})
    assert {r["site"] for r in p.dataset} == {"a"}
    p, *_ = await go(monkeypatch, boards, {**INP, "sites": ["a", "b"]}, platform=Platform(named=named))
    assert {r["site"] for r in p.dataset} == {"b"} and len(p.dataset) == 2


async def test_prune_after_90_days_and_refresh_of_active_postings(monkeypatch):
    today = today_days()
    named = {STORE_NAME: {record_key("default"): {"version": 1, "seen": {
        "acme|p1": today - 200,          # still on the board: refreshed, never forgotten
        "acme|gone-old": today - (PRUNE_AFTER_DAYS + 1),   # off the board for > 90 days: pruned
        "acme|gone-recent": today - (PRUNE_AFTER_DAYS - 1),  # off the board, young enough: kept
    }}}}
    p, *_ = await go(monkeypatch, {("global", "acme"): board(2)}, INP, platform=Platform(named=named))
    assert ids(p) == ["p2"]  # p1 stays suppressed although its stamp was 200 days old
    seen = store(p)["seen"]
    assert seen["acme|p1"] == today and seen["acme|p2"] == today
    assert "acme|gone-old" not in seen and seen["acme|gone-recent"] == today - (PRUNE_AFTER_DAYS - 1)
    assert S(p)["incremental"]["pruned"] == 1


async def test_seen_store_unit_prune_boundary():
    class KV:
        d = {}
        async def get_value(self, k, default_value=None): return self.d.get(k, default_value)
        async def set_value(self, k, v): self.d[k] = v
    s = SeenStore(KV(), "k", today=1000)
    s.seen = {"a": 1000 - PRUNE_AFTER_DAYS, "b": 1000 - PRUNE_AFTER_DAYS - 1}
    assert s.prune() == 1 and list(s.seen) == ["a"]


async def test_discover_mode_never_touches_the_state(monkeypatch):
    p, *_ = await go(monkeypatch, {("global", "acme"): board(2)}, {**INP, "discoverFilters": True})
    assert len(p.dataset) == 1 and p.named == {} and S(p)["incremental"] == {"enabled": False}


async def test_restart_does_not_redeliver_and_state_catches_up(monkeypatch):
    """The run dies mid-way; the restarted run (same default storage) neither repeats nor loses postings."""
    boards = {("global", "acme"): board(60)}
    p = Platform(crash_on_push=31)
    with pytest.raises(RuntimeError):
        await go(monkeypatch, boards, {**INP, "maxItems": 40}, platform=p)
    assert len(p.dataset) == 30
    assert len(store(p)["seen"]) == 30  # a failed push still flushes what was delivered
    # a hard kill (migration) skips that flush: only the last periodic save (25 postings) survives
    rec = store(p)
    rec["seen"] = {k: v for k, v in list(rec["seen"].items())[:25]}
    p.crash_on_push = None
    await go(monkeypatch, boards, {**INP, "maxItems": 40}, platform=p)
    got = ids(p)
    assert len(got) == 40 and len(set(got)) == 40 and got == [f"p{i}" for i in range(1, 41)]
    assert S(p)["resumed"] and S(p)["recordsPushedThisRun"] == 10
    assert len(store(p)["seen"]) == 40  # the named state caught up with the dataset
    # the next scheduled run (fresh default storage) sees only the 20 postings never delivered
    p2, *_ = await go(monkeypatch, boards, {**INP, "maxItems": 100}, platform=Platform(named=p.named))
    assert ids(p2) == [f"p{i}" for i in range(41, 61)]
