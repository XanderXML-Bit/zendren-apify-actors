"""Offline tests for src.main.run: charging gate, budget, restarts (no network)."""

import logging
from decimal import Decimal as D

import pytest

import src.pipeline as pl
from src.ats import Job
from src.main import run
from tests.fakes import FakeActor, Platform

PRICES = {"actor-start": D("0.01"), "job-scraped": D("0.003")}
INP = {"companies": ["lever:acme"], "maxItems": 100, "includeDescription": False}


@pytest.fixture(autouse=True)
def fake_source(monkeypatch):
    """One company with 60 jobs; records how often a source is crawled."""
    calls = []

    async def fake(client, ref, flt, include_description=True, report=None):
        calls.append(ref.slug)
        for i in range(60):
            yield Job(title=f"Job {i}", company="Acme", location="X", remote=False, department=None,
                      postedDate=None, applyUrl=None, atsSource=ref.ats, descriptionText=None,
                      jobId=str(i), companySlug=ref.slug)

    monkeypatch.setattr(pl, "fetch_company", fake)
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)
    return calls


def _ids(p):
    return [j["jobId"] for j in p.dataset]


async def go(p, inp=None, **kw):
    a = FakeActor(p, {**INP, **(inp or {})})
    await run(a)
    return a


async def test_no_valid_companies_fails_with_summary(fake_source):
    p = Platform()
    a = await go(p, {"companies": ["foo:bar", "https://example.com/jobs"]})
    assert a.failed and "No valid companies" in a.failed and p.dataset == [] and fake_source == []
    assert p.kv["SUMMARY"]["invalidInputs"] == ["foo:bar", "https://example.com/jobs"]


# ---------- CHARGING_ENABLED gate ----------
@pytest.mark.parametrize("value", [None, "0", "true", ""])
async def test_gate_off_never_charges(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("CHARGING_ENABLED", value)
    p = Platform(prices=dict(PRICES), max_total=D("1"))
    await go(p, {"maxItems": 7})
    assert len(p.dataset) == 7
    assert p.charge_calls == [] and not p.charged


async def test_gate_on_charges_start_once_and_every_job(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    a = await go(p, {"maxItems": 12})
    assert len(p.dataset) == 12
    assert p.charged["actor-start"] == 1 and p.charged["job-scraped"] == 12
    summary = p.kv["SUMMARY"]
    assert summary["chargingEnabled"] and summary["jobsPushed"] == 12
    assert summary["stoppedReason"] == "maxItems"
    assert not a.failed


async def test_gate_on_non_ppe_run_is_not_stopped(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices={}, ppe=False)
    await go(p, {"maxItems": 9})
    assert len(p.dataset) == 9 and not p.charged


# ---------- budget ----------
async def test_exhausted_budget_before_start_scrapes_and_pushes_nothing(monkeypatch, fake_source):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("0"))
    await go(p)
    assert fake_source == [] and p.dataset == []  # no company was even fetched
    assert p.kv["SUMMARY"]["stoppedReason"] == "budget"


async def test_budget_only_covers_start_fee(monkeypatch, fake_source):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("0.01"))
    await go(p)
    assert fake_source == [] and p.dataset == []
    assert p.charged["actor-start"] == 1 and p.charged["job-scraped"] == 0
    assert p.kv["SUMMARY"]["stoppedReason"] == "budget"


async def test_budget_stops_exactly_at_limit_and_never_pushes_unbillable(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("0.019"))  # start 0.01 + 3 jobs (0.009)
    await go(p, {"maxItems": 50})
    assert len(p.dataset) == 3 and p.charged["job-scraped"] == 3
    assert len(p.dataset) == p.charged["job-scraped"]  # every pushed item was billed
    assert p.spent() <= p.max_total
    assert p.kv["SUMMARY"]["stoppedReason"] == "budget"


async def test_budget_accounts_for_synthetic_dataset_item_event(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    prices = {**PRICES, "apify-default-dataset-item": D("0.001")}
    p = Platform(prices=prices, max_total=D("0.018"))  # (0.018-0.01)/(0.003+0.001) = 2 jobs
    await go(p, {"maxItems": 50})
    assert len(p.dataset) == 2 == p.charged["job-scraped"]
    assert p.kv["SUMMARY"]["stoppedReason"] == "budget"


async def test_stops_when_push_reports_zero_charged(monkeypatch):
    """Capacity vanishes mid-run (e.g. other charges): push_data returns charged_count=0."""
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    a = FakeActor(p, {**INP, "maxItems": 50})
    real_push = a.push_data

    async def push(data, *, charged_event_name=None):
        if len(p.dataset) == 4:
            p.max_total = p.spent()  # budget gone
        return await real_push(data, charged_event_name=charged_event_name)

    a.push_data = push
    await run(a)
    assert len(p.dataset) == 4 == p.charged["job-scraped"]
    assert p.kv["SUMMARY"]["stoppedReason"] == "budget"


# ---------- synthetic events warning ----------
async def test_warns_when_dataset_item_event_enabled(monkeypatch, caplog):
    p = Platform(prices={**PRICES, "apify-default-dataset-item": D("0.0001")})
    with caplog.at_level(logging.WARNING):
        await go(p, {"maxItems": 1})
    assert "apify-default-dataset-item" in caplog.text and "OFF" in caplog.text


async def test_no_warning_for_apify_actor_start(caplog):
    """The synthetic start event is part of the pricing now; it must not be flagged."""
    p = Platform(prices={**PRICES, "apify-actor-start": D("0.00005")})
    with caplog.at_level(logging.WARNING):
        await go(p, {"maxItems": 1})
    assert "look ENABLED" not in caplog.text and "apify-actor-start" not in caplog.text


async def test_no_builtin_warning_when_disabled(caplog):
    p = Platform(prices=dict(PRICES))
    with caplog.at_level(logging.WARNING):
        await go(p, {"maxItems": 1})
    assert "look ENABLED" not in caplog.text


# ---------- restarts ----------
async def test_restart_after_crash_is_idempotent(monkeypatch, fake_source):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_on_push = 31  # 30 jobs delivered; state was last persisted at 25
    first = FakeActor(p, {**INP, "maxItems": 40})
    with pytest.raises(RuntimeError):
        await run(first)
    assert len(p.dataset) == 30 and p.charged["job-scraped"] == 30
    assert p.kv["JOBFEED_STATE"]["pushed"] == 25  # older than the dataset

    p.crash_on_push = None
    await go(p, {"maxItems": 40})  # restarted run, same platform storage
    ids = _ids(p)
    assert len(ids) == 40 and len(set(ids)) == 40  # no duplicates, maxItems honoured across restart
    assert p.charged["actor-start"] == 1  # start fee not charged again
    assert p.charged["job-scraped"] == 40  # no job billed twice
    summary = p.kv["SUMMARY"]
    assert summary["resumed"] and summary["jobsPushed"] == 40 and summary["jobsPushedThisRun"] == 10


async def test_restart_with_lost_state_still_does_not_recharge_start(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.charged["actor-start"] = 1  # platform remembers it, our state record is gone
    await go(p, {"maxItems": 3})
    assert p.charged["actor-start"] == 1 and p.charged["job-scraped"] == 3


async def test_restart_after_completion_does_nothing(monkeypatch, fake_source):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    await go(p, {"maxItems": 5})
    fake_source.clear()
    before = dict(p.charged)
    await go(p, {"maxItems": 5})
    assert fake_source == [] and dict(p.charged) == before and len(p.dataset) == 5
    assert p.kv["SUMMARY"]["stoppedReason"] == "maxItems"


async def test_restart_respects_budget_left(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("0.019"))
    await go(p, {"maxItems": 50})
    assert len(p.dataset) == 3
    await go(p, {"maxItems": 50})
    assert len(p.dataset) == 3 and p.charged["job-scraped"] == 3 and p.charged["actor-start"] == 1


async def test_fake_matches_sdk_surface():
    """Guard the fakes against drifting from the installed SDK's API."""
    from apify import Actor
    from apify._charging import ChargingManager
    for name in ("get_input", "get_charging_manager", "use_state", "open_key_value_store",
                 "open_dataset", "set_value", "set_status_message", "fail", "charge", "push_data",
                 "on", "off"):
        assert hasattr(Actor, name), name
    for name in ("get_pricing_info", "get_charged_event_count", "compute_push_data_limit", "charge"):
        assert hasattr(ChargingManager, name), name
