"""Offline regression tests for review round 2: H1 (write-then-charge gap), M3-M5, L1."""

import asyncio
from decimal import Decimal as D

import pytest

import src.main as main_mod
import src.pipeline as pl
from src.ats import Job
from src.main import run
from tests.fakes import FakeActor, Platform

PRICES = {"actor-start": D("0.01"), "job-scraped": D("0.003")}
INP = {"companies": ["lever:acme"], "maxItems": 10, "includeDescription": False}


def make_job(ref, i):
    return Job(title=f"Job {i}", company="Acme", location="X", remote=False, department=None,
               postedDate=None, applyUrl=None, atsSource=ref.ats, descriptionText=None,
               jobId=str(i), companySlug=ref.slug)


@pytest.fixture(autouse=True)
def fake_source(monkeypatch):
    calls = []

    async def fake(client, ref, flt, include_description=True, report=None):
        calls.append(ref.slug)
        for i in range(60):
            yield make_job(ref, i)

    monkeypatch.setattr(pl, "fetch_company", fake)
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    return calls


async def go(p, inp=None):
    a = FakeActor(p, {**INP, **(inp or {})})
    await run(a)
    return a


def assert_billed_once(p):
    ids = [j["jobId"] for j in p.dataset]
    assert len(ids) == len(set(ids))
    assert p.charged["job-scraped"] == len(ids)


# ---------- H1 ----------
# charge call #1 is the start fee; job k is charged by call k+1.
async def test_crash_after_dataset_write_is_listed_unbilled_not_recharged_on_restart():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_after_write_on_push = 6  # row 6 is written, then the process dies
    with pytest.raises(RuntimeError):
        await go(p)
    assert len(p.dataset) == 6 and p.charged["job-scraped"] == 5  # the unbilled-row gap
    assert p.kv["JOBFEED_PENDING"]["pending"] == "lever|acme|5"

    await go(p, {"maxItems": 10})  # restart: the ambiguous row is counted delivered, never charged
    assert len(p.dataset) == 10
    assert p.charged["job-scraped"] == 9  # strict under-billing: exactly the one ambiguous row
    assert list(p.kv["SUMMARY"]["unbilledJobs"]) == ["lever|acme|5"]
    assert p.charged["actor-start"] == 1
    assert p.kv["JOBFEED_PENDING"] == {"pending": None}


async def test_charge_error_after_write_is_never_retried_in_process_or_on_restart():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.fail_charge_calls = {7}  # the SDK's own charge inside push_data(row 6) raises after the write
    with pytest.raises(RuntimeError):
        await go(p)
    assert len(p.dataset) == 6 and p.charged["job-scraped"] == 5
    await go(p, {"maxItems": 10})  # restart
    assert len(p.dataset) == 10
    assert p.charged["job-scraped"] == 9  # the ambiguous row stays unbilled
    assert list(p.kv["SUMMARY"]["unbilledJobs"]) == ["lever|acme|5"]
    assert p.charged["job-scraped"] <= len(p.dataset)


async def test_crash_after_charge_but_before_marking_does_not_double_charge(monkeypatch):
    """The charge went through, then the process died: the pending key must not trigger a re-charge."""
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    a = FakeActor(p, {**INP})
    real = a.push_data

    async def push(data, *, charged_event_name=None):
        res = await real(data, charged_event_name=charged_event_name)
        if len(p.dataset) == 4:
            raise RuntimeError("died after the charge")
        return res

    a.push_data = push
    p.fail_charge_calls = {99}
    with pytest.raises(RuntimeError):
        await run(a)
    assert len(p.dataset) == 4 and p.charged["job-scraped"] == 4
    await go(p, {"maxItems": 10})
    assert len(p.dataset) == 10
    assert_billed_once(p)


async def test_crash_before_write_redelivers_job_once():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_on_push = 4
    with pytest.raises(RuntimeError):
        await go(p)
    assert len(p.dataset) == 3
    p.crash_on_push = None
    await go(p)
    assert len(p.dataset) == 10
    assert_billed_once(p)


async def test_ambiguous_row_is_reported_not_silent_even_with_budget_left():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_after_write_on_push = 5
    with pytest.raises(RuntimeError):
        await go(p)
    assert len(p.dataset) == 5 and p.charged["job-scraped"] == 4
    p.crash_after_write_on_push = None
    await go(p)
    summary = p.kv["SUMMARY"]
    assert list(summary["unbilledJobs"]) == ["lever|acme|4"]
    assert p.charged["job-scraped"] == 9 and len(p.dataset) == 10


# ---------- L1 ----------
async def test_per_company_counts_rebuilt_from_dataset_after_crash():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_on_push = 31  # 30 delivered, state persisted at 25
    with pytest.raises(RuntimeError):
        await go(p, {"maxItems": 40})
    assert p.kv["JOBFEED_STATE"]["pushed"] == 25
    p.crash_on_push = None
    await go(p, {"maxItems": 40})
    summary = p.kv["SUMMARY"]
    assert summary["jobsPushed"] == 40 and summary["perCompany"] == {
        "lever:acme": {"count": 40, "reason": "cap_reached"}}
    assert summary["perCompanyCounts"] == {"lever:acme": 40}
    assert_billed_once(p)


# ---------- M3 ----------
async def test_notes_survive_restart_after_completion(fake_source):
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.kv["JOBFEED_STATE"] = {
        "seen": [f"lever|acme|{i}" for i in range(10)], "pushed": 10, "startCharged": True,
        "perCompany": {"lever:acme": 10}, "failed": {},
        "notes": {"truncatedCompanies": {"workday:x.wd5/S": "cap"},
                  "detailFailures": {"workday:x.wd5/S": 3},
                  "unverifiedJobsIncluded": {"workday:x.wd5/S": 2}}}
    p.dataset.extend({"atsSource": "lever", "companySlug": "acme", "jobId": str(i)} for i in range(10))
    p.charged["actor-start"] = 1
    p.charged["job-scraped"] = 10
    await go(p)
    assert fake_source == []  # nothing scraped
    s = p.kv["SUMMARY"]
    assert s["truncatedCompanies"] == {"workday:x.wd5/S": "cap"}
    assert s["detailFailures"] == {"workday:x.wd5/S": 3}
    assert s["unverifiedJobsIncluded"] == {"workday:x.wd5/S": 2}
    assert p.kv["JOBFEED_STATE"]["notes"]["detailFailures"] == {"workday:x.wd5/S": 3}


async def test_notes_merged_with_new_report_and_persisted_during_crawl(monkeypatch):
    async def fake(client, ref, flt, include_description=True, report=None):
        report.truncated[ref.key] = "cap hit"
        report.detail_failures[ref.key] += 2
        for i in range(60):
            yield make_job(ref, i)

    monkeypatch.setattr(pl, "fetch_company", fake)
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_on_push = 28  # dies mid-crawl; only the checkpoint at 25 was saved
    with pytest.raises(RuntimeError):
        await go(p, {"maxItems": 40})
    saved = p.kv["JOBFEED_STATE"]["notes"]
    assert saved["truncatedCompanies"] == {"lever:acme": "cap hit"}  # persisted before completion
    assert saved["detailFailures"] == {"lever:acme": 2}

    p.crash_on_push = None
    await go(p, {"maxItems": 40})
    s = p.kv["SUMMARY"]
    assert s["truncatedCompanies"] == {"lever:acme": "cap hit"}  # not duplicated
    assert s["detailFailures"] == {"lever:acme": 4}  # cumulative attempts across the restart


async def test_saving_notes_repeatedly_does_not_double_count(monkeypatch):
    async def fake(client, ref, flt, include_description=True, report=None):
        report.detail_failures[ref.key] += 1
        for i in range(60):
            yield make_job(ref, i)

    monkeypatch.setattr(pl, "fetch_company", fake)
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    await go(p, {"maxItems": 60})  # several checkpoints + final save
    assert p.kv["SUMMARY"]["detailFailures"] == {"lever:acme": 1}


# ---------- M4 ----------
async def test_budget_break_stops_workers_before_client_closes(monkeypatch):
    events = []

    async def fake(client, ref, flt, include_description=True, report=None):
        try:
            if ref.slug == "slow":
                await asyncio.sleep(30)  # still fetching when the budget ends
            for i in range(60):
                yield make_job(ref, i)
        finally:
            events.append(f"source {ref.slug} finalized")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            events.append("client closed")

    monkeypatch.setattr(pl, "fetch_company", fake)
    monkeypatch.setattr(main_mod, "RateLimitedClient", Client)
    p = Platform(prices=dict(PRICES), max_total=D("0.013"))  # start + exactly one job
    await go(p, {"companies": ["lever:fast", "lever:slow"], "maxItems": 50})
    assert len(p.dataset) == 1 and p.kv["SUMMARY"]["stoppedReason"] == "budget"
    assert events[-1] == "client closed"
    assert {"source fast finalized", "source slow finalized"} <= set(events[:-1])
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert not others


# ---------- M5 ----------
@pytest.mark.parametrize("prices", [
    {"actor-start": D("0.01")},                  # job-scraped missing (old pricing)
    {"apify-actor-start": D("0.00005")},         # job-scraped missing (new pricing)
    {"actor-start": D("0.01"), "job-scraped": D("0")},
])
async def test_missing_event_price_fails_before_scraping(prices, fake_source):
    p = Platform(prices=dict(prices), max_total=D("10"))
    a = await go(p)
    assert a.failed and "refusing" in a.failed
    assert fake_source == [] and p.dataset == [] and not p.charged


async def test_missing_price_is_ignored_when_charging_disabled(monkeypatch):
    monkeypatch.delenv("CHARGING_ENABLED")
    p = Platform(prices={"actor-start": D("0.01")}, max_total=D("10"))
    a = await go(p, {"maxItems": 3})
    assert not a.failed and len(p.dataset) == 3 and not p.charged


async def test_missing_price_is_ignored_when_not_ppe():
    p = Platform(prices={}, ppe=False)
    a = await go(p, {"maxItems": 3})
    assert not a.failed and len(p.dataset) == 3
