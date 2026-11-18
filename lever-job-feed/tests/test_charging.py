"""Pay-per-event: CHARGING_ENABLED gate, budget, restarts. The platform charges `apify-actor-start`
itself (modelled by pre-charging it); this code only charges the custom `job` event."""

import logging
from decimal import Decimal as D

import pytest

from src.main import run
from tests.fakes import FakeActor, Platform
from tests.helpers import board, go, ids

START, JOB = D("0.00005"), D("0.0015")
PRICES = {"apify-actor-start": START, "job": JOB}
BOARDS = {("global", "acme"): board(60)}
INP = {"sites": ["acme"], "maxItems": 100, "descriptionFormat": "none"}


def plat(max_total="10", prices=None, **kw):
    p = Platform(prices=dict(prices or PRICES), max_total=D(max_total), **kw)
    p.charged["apify-actor-start"] = 1  # charged by the platform when the run started
    return p


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")


async def test_gate_off_never_charges(monkeypatch):
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)
    p = plat()
    await go(monkeypatch, BOARDS, {**INP, "maxItems": 7}, platform=p)
    assert len(p.dataset) == 7 and p.charge_calls == [] and p.charged["job"] == 0


@pytest.mark.parametrize("value", ["0", "true", ""])
async def test_gate_requires_exactly_1(monkeypatch, value):
    monkeypatch.setenv("CHARGING_ENABLED", value)
    p = plat()
    await go(monkeypatch, BOARDS, {**INP, "maxItems": 3}, platform=p)
    assert p.charge_calls == []


async def test_gate_on_charges_one_job_event_per_posting_and_never_the_start_event(on, monkeypatch):
    p = plat()
    _, _, a = await go(monkeypatch, BOARDS, {**INP, "maxItems": 12}, platform=p)
    assert len(p.dataset) == 12 and p.charged["job"] == 12
    assert p.charged["apify-actor-start"] == 1  # still only the platform's own charge
    assert all(ev == "job" for ev, _ in p.charge_calls)
    s = p.kv["SUMMARY"]
    assert s["chargingEnabled"] and s["recordsPushed"] == 12 and s["stoppedReason"] == "maxItems" and not a.failed


async def test_discover_charges_one_job_event_per_site_record(on, monkeypatch):
    p = plat()
    boards = {("global", "a"): board(40), ("global", "b"): board(3, "b")}
    await go(monkeypatch, boards, {"sites": ["a", "b", "ghost"], "discoverFilters": True}, platform=p)
    assert len(p.dataset) == 2 and p.charged["job"] == 2  # none for the site that failed


async def test_non_ppe_run_is_not_stopped(on, monkeypatch):
    p = Platform(prices={}, ppe=False)
    await go(monkeypatch, BOARDS, {**INP, "maxItems": 9}, platform=p)
    assert len(p.dataset) == 9 and not p.charged


async def test_missing_job_price_refuses_to_deliver(on, monkeypatch):
    p = plat(prices={"apify-actor-start": START})
    _, lever, a = await go(monkeypatch, BOARDS, INP, platform=p)
    assert a.failed and "no price for 'job'" in a.failed and p.dataset == [] and lever.requests == []


# ---------- budget ----------
async def test_exhausted_budget_fetches_and_pushes_nothing(on, monkeypatch):
    p = plat(max_total="0.00005")  # only the start event fits
    _, lever, _ = await go(monkeypatch, BOARDS, INP, platform=p)
    assert lever.requests == [] and p.dataset == []
    s = p.kv["SUMMARY"]
    assert s["stoppedReason"] == "budget" and s["sites"]["acme"]["reason"] == "budget"


async def test_budget_stops_exactly_at_limit_and_never_pushes_unbillable(on, monkeypatch):
    p = plat(max_total="0.00455")  # start 0.00005 + 3 jobs
    await go(monkeypatch, BOARDS, {**INP, "maxItems": 50}, platform=p)
    assert len(p.dataset) == 3 == p.charged["job"]
    assert p.spent() <= p.max_total
    s = p.kv["SUMMARY"]
    assert s["stoppedReason"] == "budget" and s["sites"]["acme"] == {**s["sites"]["acme"], "count": 3, "reason": "budget"}


async def test_budget_larger_than_site_is_not_reported_as_budget(on, monkeypatch):
    p = plat(max_total="0.0504")  # room for 33, only 5 postings
    await go(monkeypatch, {("global", "acme"): board(5)}, INP, platform=p)
    s = p.kv["SUMMARY"]
    assert len(p.dataset) == 5 and s["stoppedReason"] == "completed" and s["sites"]["acme"]["reason"] == "completed"


async def test_budget_split_stays_fair_across_sites(on, monkeypatch):
    p = plat(max_total="0.00605")  # 4 jobs
    boards = {("global", "a"): board(30, "a"), ("global", "b"): board(30, "b")}
    await go(monkeypatch, boards, {"sites": ["a", "b"], "maxItems": 50}, platform=p)
    assert sorted(r["site"] for r in p.dataset) == ["a", "a", "b", "b"]


async def test_stops_when_push_reports_zero_charged(on, monkeypatch):
    p = plat()
    import src.main as m
    from tests.fakes import FakeLever
    monkeypatch.setattr(m, "make_client", FakeLever(BOARDS).client)
    a = FakeActor(p, {**INP, "maxItems": 50})
    real_push = a.push_data

    async def push(data, *, charged_event_name=None):
        if len(p.dataset) == 4:
            p.max_total = p.spent()  # budget vanishes (other charges)
        return await real_push(data, charged_event_name=charged_event_name)

    a.push_data = push
    await run(a)
    assert len(p.dataset) == 4 == p.charged["job"]
    assert p.kv["SUMMARY"]["stoppedReason"] == "budget"


async def test_budget_stop_does_not_mark_unpushed_as_seen(on, monkeypatch):
    p = plat(max_total="0.00455")
    named = {}
    p.named = named
    await go(monkeypatch, BOARDS, {**INP, "onlyNewJobs": True, "maxItems": 50}, platform=p)
    assert len(p.dataset) == 3
    p2 = plat(named=named)
    await go(monkeypatch, BOARDS, {**INP, "onlyNewJobs": True, "maxItems": 5}, platform=p2)
    assert ids(p2) == ["p4", "p5", "p6", "p7", "p8"]


# ---------- synthetic dataset-item event ----------
async def test_paid_default_dataset_item_event_fails_before_fetching(on, monkeypatch):
    p = plat(prices={**PRICES, "apify-default-dataset-item": D("0.0001")})
    _, lever, a = await go(monkeypatch, BOARDS, {**INP, "maxItems": 5}, platform=p)
    assert a.failed and "apify-default-dataset-item" in a.failed and "twice" in a.failed
    assert lever.requests == [] and p.dataset == [] and p.charge_calls == []


async def test_zero_priced_dataset_item_event_is_fine(on, monkeypatch):
    p = plat(prices={**PRICES, "apify-default-dataset-item": D("0")})
    _, _, a = await go(monkeypatch, BOARDS, {**INP, "maxItems": 3}, platform=p)
    assert not a.failed and len(p.dataset) == 3


async def test_actor_start_event_is_allowed(on, monkeypatch):
    p = plat()
    assert p.prices["apify-actor-start"] > 0
    _, _, a = await go(monkeypatch, BOARDS, {**INP, "maxItems": 2}, platform=p)
    assert not a.failed and len(p.dataset) == 2


@pytest.mark.parametrize("flag", [None, "0", "true", ""])
async def test_paid_dataset_item_event_fails_closed_even_with_the_charging_gate_off(monkeypatch, flag):
    """The platform bills this synthetic event on every default-dataset write by itself, so a price on it
    means double billing whatever CHARGING_ENABLED says."""
    if flag is None:
        monkeypatch.delenv("CHARGING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CHARGING_ENABLED", flag)
    p = plat(prices={**PRICES, "apify-default-dataset-item": D("0.0001")})
    _, lever, a = await go(monkeypatch, BOARDS, {**INP, "maxItems": 1}, platform=p)
    assert a.failed and "apify-default-dataset-item" in a.failed
    assert lever.requests == [] and p.dataset == [] and p.charge_calls == []


async def test_unpriced_local_run_without_pay_per_event_is_unchanged(monkeypatch):
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)
    p = Platform(ppe=False)
    _, _, a = await go(monkeypatch, BOARDS, {**INP, "maxItems": 2}, platform=p)
    assert not a.failed and len(p.dataset) == 2


async def test_no_warning_in_the_intended_setup(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        await go(monkeypatch, BOARDS, {**INP, "maxItems": 1}, platform=plat())
    assert "ENABLED" not in caplog.text


# ---------- restarts ----------
async def test_restart_after_crash_is_idempotent(on, monkeypatch):
    p = plat(crash_on_push=31)  # 30 delivered; run state last persisted at 25
    with pytest.raises(RuntimeError):
        await go(monkeypatch, BOARDS, {**INP, "maxItems": 40}, platform=p)
    assert len(p.dataset) == 30 and p.charged["job"] == 30
    assert p.kv["LEVERFEED_STATE"]["pushed"] == 25  # older than the dataset

    p.crash_on_push = None
    await go(monkeypatch, BOARDS, {**INP, "maxItems": 40}, platform=p)
    got = ids(p)
    assert len(got) == 40 == len(set(got))
    assert p.charged["job"] == 40 and p.charged["apify-actor-start"] == 1
    s = p.kv["SUMMARY"]
    assert s["resumed"] and s["recordsPushed"] == 40 and s["recordsPushedThisRun"] == 10


async def test_crash_between_write_and_charge_is_never_charged_twice(on, monkeypatch):
    p = plat(crash_after_write_on_push=6)  # 6th record written, its charge never sent
    with pytest.raises(RuntimeError):
        await go(monkeypatch, BOARDS, {**INP, "maxItems": 10}, platform=p)
    assert len(p.dataset) == 6 and p.charged["job"] == 5
    p.crash_after_write_on_push = None
    await go(monkeypatch, BOARDS, {**INP, "maxItems": 10}, platform=p)
    got = ids(p)
    assert len(got) == 10 == len(set(got))
    assert p.charged["job"] == 9  # the ambiguous one is not re-sent, ever
    s = p.kv["SUMMARY"]
    assert list(s["unbilledJobs"]) == ["acme|p6"]


async def test_restart_after_completion_does_nothing(on, monkeypatch):
    p = plat()
    await go(monkeypatch, BOARDS, {**INP, "maxItems": 5}, platform=p)
    before = dict(p.charged)
    _, lever, _ = await go(monkeypatch, BOARDS, {**INP, "maxItems": 5}, platform=p)
    assert lever.requests == [] and dict(p.charged) == before and len(p.dataset) == 5
    s = p.kv["SUMMARY"]
    assert s["stoppedReason"] == "maxItems" and s["sites"]["acme"] == {"count": 5, "reason": "cap_reached"}


async def test_restart_respects_budget_left(on, monkeypatch):
    p = plat(max_total="0.00455")
    await go(monkeypatch, BOARDS, {**INP, "maxItems": 50}, platform=p)
    await go(monkeypatch, BOARDS, {**INP, "maxItems": 50}, platform=p)
    assert len(p.dataset) == 3 and p.charged["job"] == 3


async def test_restart_keeps_fair_share_across_sites(on, monkeypatch):
    boards = {("global", "a"): board(30, "a"), ("global", "b"): board(30, "b")}
    p = plat(crash_on_push=5)  # a1..a4 delivered (site a goes first)
    with pytest.raises(RuntimeError):
        await go(monkeypatch, boards, {"sites": ["a", "b"], "maxItems": 8}, platform=p)
    p.crash_on_push = None
    await go(monkeypatch, boards, {"sites": ["a", "b"], "maxItems": 8}, platform=p)
    per = {s: sum(1 for r in p.dataset if r["site"] == s) for s in "ab"}
    assert per == {"a": 4, "b": 4} and len(p.dataset) == 8


async def test_fake_matches_sdk_surface():
    """Guard the fakes against drifting from the installed SDK's API."""
    from apify import Actor
    from apify._charging import ChargingManager
    for name in ("get_input", "get_charging_manager", "use_state", "open_key_value_store", "open_dataset",
                 "set_value", "get_value", "set_status_message", "fail", "charge", "push_data"):
        assert hasattr(Actor, name), name
    for name in ("get_pricing_info", "get_charged_event_count", "compute_push_data_limit", "charge"):
        assert hasattr(ChargingManager, name), name
