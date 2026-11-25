"""Offline regression tests for review round 3: R3-H1 (never double-charge), R3-M1 (live notes).

The billing fakes here keep the *remote* (platform) counter apart from the *local* SDK
counter, as the real SDK does: the local one is seeded from the platform at startup and
only follows a charge whose response arrived.
"""

import asyncio
from decimal import Decimal as D

import pytest

import src.main as main_mod
import src.pipeline as pl
from src.ats import Job
from src.main import run
from tests.fakes import FakeActor, FakeChargingManager, Platform

PRICES = {"actor-start": D("0.01"), "job-scraped": D("0.003")}
INP = {"companies": ["lever:acme"], "maxItems": 10, "includeDescription": False}


def make_job(ref, i):
    return Job(title=f"Job {i}", company="Acme", location="X", remote=False, department=None,
               postedDate=None, applyUrl=None, atsSource=ref.ats, descriptionText=None,
               jobId=str(i), companySlug=ref.slug)


@pytest.fixture(autouse=True)
def fake_source(monkeypatch):
    async def fake(client, ref, flt, include_description=True, report=None):
        for i in range(60):
            yield make_job(ref, i)

    monkeypatch.setattr(pl, "fetch_company", fake)
    monkeypatch.setenv("CHARGING_ENABLED", "1")


class SplitChargingManager(FakeChargingManager):
    """Remote counter = ``p.charged``; the local one is seeded at construction and only
    updated when the billing response arrives."""

    def __init__(self, p: Platform) -> None:
        super().__init__(p)
        self.local = dict(p.charged)
        self.lose_response_on_job = None  # Nth job-scraped charge (1-based, per manager)
        self.job_charges = 0

    def get_charged_event_count(self, event):
        return self.local.get(event, 0)

    async def charge(self, event, *, count=1, idempotency_key=None):
        if event == "job-scraped":
            self.job_charges += 1
        lost = event == "job-scraped" and self.job_charges == self.lose_response_on_job
        res = await super().charge(event, count=count, idempotency_key=idempotency_key)
        if lost:
            raise RuntimeError("billing committed remotely; response lost")
        self.local[event] = self.local.get(event, 0) + res.charged_count
        return res


class SplitActor(FakeActor):
    def __init__(self, platform, inp, *, lose_response_on_job=None):
        super().__init__(platform, inp)
        self.cm = SplitChargingManager(platform)
        self.cm.lose_response_on_job = lose_response_on_job


async def go(p, inp=None, **kw):
    a = SplitActor(p, {**INP, **(inp or {})}, **kw)
    await run(a)
    return a


def assert_never_overbilled(p, *, slack=0):
    rows = len(p.dataset)
    assert len(set(j["jobId"] for j in p.dataset)) == rows
    assert rows - slack <= p.charged["job-scraped"] <= rows


@pytest.mark.parametrize("lost_job", [1, 4, 10])
async def test_lost_billing_response_is_not_retried_in_process_or_on_restart(lost_job):
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    with pytest.raises(RuntimeError):
        await go(p, lose_response_on_job=lost_job)
    # the platform accepted the charge: row and remote charge both exist, exactly once
    assert len(p.dataset) == lost_job and p.charged["job-scraped"] == lost_job
    await go(p)  # restart: the platform-seeded count shows the pending row as paid
    assert len(p.dataset) == 10
    assert p.charged["job-scraped"] == 10
    assert p.charged["actor-start"] == 1
    assert_never_overbilled(p)


async def test_lost_billing_response_with_repeated_failures_never_exceeds_rows():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    for lost_job in (3, 2, 2):  # three attempts, each losing a response
        with pytest.raises(RuntimeError):
            await go(p, {"maxItems": 20}, lose_response_on_job=lost_job)
        assert_never_overbilled(p)
    await go(p, {"maxItems": 20})
    assert len(p.dataset) == 20
    assert_never_overbilled(p)
    assert p.charged["job-scraped"] == 20


async def test_charge_that_never_landed_is_not_resent_on_restart():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.fail_charge_calls = {4}  # start + 2 jobs fine; the charge of job 3 fails before billing
    with pytest.raises(RuntimeError):
        await go(p)
    assert len(p.dataset) == 3 and p.charged["job-scraped"] == 2
    await go(p)
    assert len(p.dataset) == 10 and p.charged["job-scraped"] == 9  # job 3 is possibly unbilled
    assert list(p.kv["SUMMARY"]["unbilledJobs"]) == ["lever|acme|2"]
    # and a further restart of the finished run charges nothing
    await go(p)
    assert p.charged["job-scraped"] == 9
    assert list(p.kv["SUMMARY"]["unbilledJobs"]) == ["lever|acme|2"]  # still reported


async def test_restart_never_charges_for_pending_row_or_shortfall():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.dataset.extend({"atsSource": "lever", "companySlug": "acme", "jobId": str(i)} for i in range(5))
    p.kv["JOBFEED_PENDING"] = {"pending": "lever|acme|4"}
    p.charged["actor-start"] = 1
    p.charged["job-scraped"] = 2  # behind, but nothing is ever re-charged
    await go(p, {"maxItems": 5})
    assert p.charged["job-scraped"] == 2
    assert list(p.kv["SUMMARY"]["unbilledJobs"]) == ["lever|acme|4"]
    assert p.kv["SUMMARY"]["jobsPushed"] == 5


async def test_no_job_charge_is_issued_after_an_in_process_failure():
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    p.crash_after_write_on_push = 3
    with pytest.raises(RuntimeError):
        await go(p)
    assert [c for c in p.charge_calls if c[0] == "job-scraped"] == [("job-scraped", 1)] * 2


# ---------- R3-M1 ----------
async def test_autosave_and_migration_before_checkpoint_keeps_current_notes(monkeypatch):
    async def noisy(client, ref, flt, include_description=True, report=None):
        report.detail_failures[ref.key] += 2
        report.unverified[ref.key] += 1
        report.truncated[ref.key] = "cap"
        for i in range(60):
            yield make_job(ref, i)

    monkeypatch.setattr(pl, "fetch_company", noisy)
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    a = SplitActor(p, {**INP, "maxItems": 20})
    original_set = a.set_value

    async def set_value(key, value, **kw):
        if key == main_mod.PENDING_KEY and len(p.dataset) == 10:
            await main_mod._persist(a)  # SDK autosave right before the migration
            raise asyncio.CancelledError("migration")
        await original_set(key, value, **kw)

    a.set_value = set_value
    with pytest.raises(asyncio.CancelledError):
        await run(a)
    saved = p.kv[main_mod.STATE_KEY]
    assert saved["pushed"] == 10  # no 25-row checkpoint was reached
    assert saved["notes"]["detailFailures"] == {"lever:acme": 2}
    assert saved["notes"]["unverifiedJobsIncluded"] == {"lever:acme": 1}
    assert saved["notes"]["truncatedCompanies"] == {"lever:acme": "cap"}

    async def quiet(client, ref, flt, include_description=True, report=None):
        for i in range(60):
            yield make_job(ref, i)

    monkeypatch.setattr(pl, "fetch_company", quiet)
    await go(p, {"maxItems": 20})
    s = p.kv["SUMMARY"]
    assert s["detailFailures"] == {"lever:acme": 2}
    assert s["unverifiedJobsIncluded"] == {"lever:acme": 1}
    assert s["truncatedCompanies"] == {"lever:acme": "cap"}


async def test_notes_reach_state_even_when_no_job_is_delivered(monkeypatch):
    async def only_warnings(client, ref, flt, include_description=True, report=None):
        report.detail_failures[ref.key] += 1
        report.detail_failures[ref.key] += 1
        raise RuntimeError("boom")
        yield  # pragma: no cover

    monkeypatch.setattr(pl, "fetch_company", only_warnings)
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    a = SplitActor(p, dict(INP))
    await run(a)
    # not just at the end: the live state object already carries the notes
    assert a.states[main_mod.STATE_KEY]["notes"]["detailFailures"] == {"lever:acme": 2}
    assert p.kv["SUMMARY"]["detailFailures"] == {"lever:acme": 2}


def test_report_change_hook_fires_for_all_containers():
    from src.ats import RunReport
    r = RunReport()
    seen = []
    r.on_change = lambda: seen.append(r.to_dict())
    r.truncated["a"] = "x"
    r.detail_failures["a"] += 1
    r.unverified["a"] += 1
    assert len(seen) == 3 and seen[-1]["unverifiedJobsIncluded"] == {"a": 1}
