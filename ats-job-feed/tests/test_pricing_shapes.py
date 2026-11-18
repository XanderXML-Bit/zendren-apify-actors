"""The actor must work under the previous pricing (custom ``actor-start`` + ``job-scraped``)
and the current one (platform-charged ``apify-actor-start`` + ``job-scraped``)."""

from decimal import Decimal as D

import pytest

import src.pipeline as pl
from src.ats import Job
from src.main import run, start_event_priced
from tests.fakes import FakeActor, Platform

OLD = {"actor-start": D("0.01"), "job-scraped": D("0.003")}
NEW = {"apify-actor-start": D("0.00005"), "job-scraped": D("0.0015")}
INP = {"companies": ["lever:acme"], "maxItems": 100, "includeDescription": False}


@pytest.fixture(autouse=True)
def fake_source(monkeypatch):
    async def fake(client, ref, flt, include_description=True, report=None):
        for i in range(60):
            yield Job(title=f"Job {i}", company="Acme", location="X", remote=False, department=None,
                      postedDate=None, applyUrl=None, atsSource=ref.ats, descriptionText=None,
                      jobId=str(i), companySlug=ref.slug)

    monkeypatch.setattr(pl, "fetch_company", fake)
    monkeypatch.setenv("CHARGING_ENABLED", "1")


def new_platform(max_total="10", **kw) -> Platform:
    p = Platform(prices=dict(NEW), max_total=D(max_total), **kw)
    p.charged["apify-actor-start"] = 1  # the platform bills it before our code starts
    return p


async def go(p, inp=None):
    a = FakeActor(p, {**INP, **(inp or {})})
    await run(a)
    return a


def test_start_event_priced():
    assert start_event_priced(OLD) and not start_event_priced(NEW)
    assert not start_event_priced({"actor-start": D("0")}) and not start_event_priced(None)


async def test_old_pricing_charges_start_once_and_jobs():
    p = Platform(prices=dict(OLD), max_total=D("10"))
    a = await go(p, {"maxItems": 8})
    assert not a.failed and len(p.dataset) == 8
    assert p.charged["actor-start"] == 1 and p.charged["job-scraped"] == 8


async def test_new_pricing_never_charges_custom_start():
    p = new_platform()
    a = await go(p, {"maxItems": 8})
    assert not a.failed and len(p.dataset) == 8
    assert p.charged["job-scraped"] == 8
    assert p.charged["actor-start"] == 0
    assert all(event != "actor-start" for event, _ in p.charge_calls)
    assert p.kv["SUMMARY"]["jobsPushed"] == 8 and p.kv["SUMMARY"]["stoppedReason"] == "maxItems"


async def test_zero_priced_start_event_is_not_charged():
    p = Platform(prices={"actor-start": D("0"), **NEW}, max_total=D("10"))
    await go(p, {"maxItems": 3})
    assert p.charged["actor-start"] == 0 and p.charged["job-scraped"] == 3


async def test_new_pricing_budget_counts_platform_start_fee():
    # 0.00005 (platform start, already spent) + 4 jobs at 0.0015
    p = new_platform(max_total=str(D("0.00005") + D("0.0015") * 4))
    await go(p, {"maxItems": 50})
    assert len(p.dataset) == 4 == p.charged["job-scraped"]
    assert p.spent() <= p.max_total
    assert p.kv["SUMMARY"]["stoppedReason"] == "budget"


async def test_new_pricing_budget_left_for_no_job_pushes_nothing(fake_source):
    p = new_platform(max_total="0.00005")
    await go(p, {"maxItems": 50})
    assert p.dataset == [] and p.kv["SUMMARY"]["stoppedReason"] == "budget"


async def test_new_pricing_without_job_price_is_refused():
    p = Platform(prices={"apify-actor-start": D("0.00005")}, max_total=D("10"))
    a = await go(p)
    assert a.failed and "refusing" in a.failed and p.dataset == []


async def test_new_pricing_dataset_item_event_still_warns(caplog):
    import logging
    p = new_platform()
    p.prices["apify-default-dataset-item"] = D("0.0001")
    with caplog.at_level(logging.WARNING):
        await go(p, {"maxItems": 1})
    assert "apify-default-dataset-item" in caplog.text and "OFF" in caplog.text


async def test_new_pricing_restart_does_not_duplicate_or_charge_start():
    p = new_platform()
    p.crash_on_push = 31
    first = FakeActor(p, {**INP, "maxItems": 40})
    with pytest.raises(RuntimeError):
        await run(first)
    p.crash_on_push = None
    await go(p, {"maxItems": 40})
    ids = [j["jobId"] for j in p.dataset]
    assert len(ids) == 40 == len(set(ids))
    assert p.charged["job-scraped"] == 40 and p.charged["actor-start"] == 0


async def test_charging_gate_off_never_charges_under_new_pricing(monkeypatch):
    monkeypatch.delenv("CHARGING_ENABLED")
    p = new_platform()
    await go(p, {"maxItems": 5})
    assert len(p.dataset) == 5 and p.charge_calls == []
