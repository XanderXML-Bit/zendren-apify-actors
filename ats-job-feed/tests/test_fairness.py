"""Offline tests: maxItems is split fairly across companies; SUMMARY.perCompany lists them all."""

import asyncio
from decimal import Decimal as D

import pytest

import src.pipeline as pl
from src.ats import CompanyRef, Job, JobFilter
from src.ats.models import AtsError
from src.main import run
from src.pipeline import fair_caps, iter_jobs
from tests.fakes import FakeActor, Platform

PRICES = {"actor-start": D("0.01"), "job-scraped": D("0.003")}


def make_job(ref, i):
    return Job(title=f"Job {i}", company=ref.slug, location="X", remote=False, department=None,
               postedDate=None, applyUrl=None, atsSource=ref.ats, descriptionText=None,
               jobId=f"{ref.slug}-{i}", companySlug=ref.slug)


@pytest.fixture
def source(monkeypatch):
    """Per-slug behaviour: ``spec[slug] = n`` jobs, ``"boom"`` (raises at once),
    ``("fail_after", n)`` or ``("wait", event, n)`` (blocks until the event is set).
    Unlisted slugs have 1000 jobs."""
    spec: dict = {}

    async def fake(client, ref, flt, include_description=True, report=None):
        what = spec.get(ref.slug, 1000)
        n = what
        if what == "boom":
            raise AtsError("kaput")
        if isinstance(what, tuple) and what[0] == "wait":
            await what[1].wait()
            n = what[2]
        if isinstance(what, tuple) and what[0] == "fail_after":
            n = what[1]
        for i in range(n):
            yield make_job(ref, i)
            await asyncio.sleep(0)
        if isinstance(what, tuple) and what[0] == "fail_after":
            raise AtsError("late failure")

    monkeypatch.setattr(pl, "fetch_company", fake)
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)
    return spec


def refs_of(*slugs):
    return [CompanyRef("lever", s) for s in slugs]


async def collect(refs, **kw):
    out = []
    async for j in iter_jobs(None, refs, JobFilter(), **kw):
        out.append(j)
    return out


def tally(jobs):
    t: dict[str, int] = {}
    for j in jobs:
        t[j.companySlug] = t.get(j.companySlug, 0) + 1
    return t


async def go(p, companies, max_items, **extra):
    a = FakeActor(p, {"companies": [f"lever:{c}" for c in companies], "maxItems": max_items,
                      "includeDescription": False, **extra})
    await run(a)
    return a


def per_company(p):
    return p.kv["SUMMARY"]["perCompany"]


# ---------- fair_caps ----------
def test_fair_caps_even_split_and_remainder_is_first_come():
    assert fair_caps([0, 0, 0], [False] * 3, 9) == [3, 3, 3]
    # 10 over 3: nobody has claimed a spare slot yet, so each may still take it ...
    assert fair_caps([0, 0, 0], [False] * 3, 10) == [4, 4, 4]
    # ... once one has, the others are held to the plain share
    assert fair_caps([4, 0, 0], [False] * 3, 10) == [4, 3, 3]


def test_fair_caps_leftover_of_finished_company_goes_to_the_rest():
    assert fair_caps([1, 0, 0], [True, False, False], 10) == [1, 5, 5]
    assert fair_caps([0, 0, 0, 0], [True, True, False, False], 8) == [0, 0, 4, 4]


def test_fair_caps_company_over_its_share_is_frozen_not_shrunk():
    # a restart where one company already holds 8 of 12
    assert fair_caps([8, 1, 1], [False] * 3, 12) == [8, 2, 2]


# ---------- pipeline ----------
async def test_every_company_gets_a_share_when_max_items_covers_them(source):
    refs = refs_of(*"abcdefghij")
    out = await collect(refs, max_items=10)
    assert tally(out) == {c: 1 for c in "abcdefghij"}
    out = await collect(refs, max_items=25)
    t = tally(out)
    assert sum(t.values()) == 25 and set(t.values()) <= {2, 3} and len(t) == 10


async def test_max_items_below_company_count_still_delivers_max_items(source):
    out = await collect(refs_of(*"abcde"), max_items=3)
    t = tally(out)
    assert sum(t.values()) == 3 and max(t.values()) == 1


async def test_leftover_of_short_companies_is_redistributed(source):
    source.update(a=2, b=3)
    out = await collect(refs_of("a", "b", "c", "d"), max_items=20)
    t = tally(out)
    assert t["a"] == 2 and t["b"] == 3
    assert sorted((t["c"], t["d"])) == [7, 8] and sum(t.values()) == 20


async def test_leftover_of_empty_and_failing_companies_is_redistributed(source):
    source.update(empty=0, boom="boom", late=("fail_after", 1))
    out = await collect(refs_of("empty", "boom", "late", "big1", "big2"), max_items=21)
    t = tally(out)
    assert "empty" not in t and "boom" not in t and t["late"] == 1
    assert t["big1"] + t["big2"] == 20 and abs(t["big1"] - t["big2"]) <= 1


async def test_everything_is_delivered_when_total_supply_is_below_max_items(source):
    source.update(a=2, b=3)
    out = await collect(refs_of("a", "b"), max_items=100)
    assert tally(out) == {"a": 2, "b": 3}


async def test_slow_company_does_not_block_others_and_keeps_its_share(source):
    release = asyncio.Event()
    source.update(slow=("wait", release, 100))
    got = []
    agen = iter_jobs(None, refs_of("slow", "f1", "f2"), JobFilter(), max_items=9)
    # fast companies push their share (3 each) while the slow one has produced nothing ...
    for _ in range(6):
        got.append(await asyncio.wait_for(agen.__anext__(), timeout=2))
    assert tally(got) == {"f1": 3, "f2": 3}
    # ... and their surplus is held back for the slow company's reserved share
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agen.__anext__(), timeout=0.2)
    # (wait_for cancelled the generator mid-step; start over on a fresh one to finish the run)
    await agen.aclose()
    release.set()
    out = await collect(refs_of("slow", "f1", "f2"), max_items=9)
    assert tally(out) == {"slow": 3, "f1": 3, "f2": 3}


async def test_slow_company_with_few_jobs_hands_leftover_to_fast_ones(source):
    release = asyncio.Event()
    source.update(slow=("wait", release, 1))
    refs = refs_of("slow", "f1", "f2")
    got = []
    async for j in iter_jobs(None, refs, JobFilter(), max_items=9):
        got.append(j.companySlug)
        if len(got) == 6:  # fast ones hit their share first, then the slow one is released
            assert got.count("slow") == 0
            release.set()
    t = tally([type("J", (), {"companySlug": s}) for s in got])
    assert t["slow"] == 1 and t["f1"] + t["f2"] == 8 and abs(t["f1"] - t["f2"]) <= 0


async def test_prior_counts_toward_the_share(source):
    refs = refs_of("a", "b", "c")
    out = await collect(refs, max_items=3, prior=[5, 1, 0])  # total 9
    assert tally(out) == {"b": 1, "c": 2}


async def test_outcomes_distinguish_completed_error_and_stopped(source):
    source.update(few=2, boom="boom", none=0)
    outcomes: dict[str, str] = {}
    out = await collect(refs_of("few", "boom", "none", "big"), max_items=10, outcomes=outcomes)
    assert tally(out) == {"few": 2, "big": 8}
    assert outcomes == {"lever:few": "completed", "lever:boom": "error",
                        "lever:none": "completed", "lever:big": "stopped"}


async def test_company_ending_exactly_at_the_cap_is_completed_not_stopped(source):
    source.update(a=2, b=2)
    outcomes: dict[str, str] = {}
    async for _ in iter_jobs(None, refs_of("a", "b"), JobFilter(), max_items=4, outcomes=outcomes):
        await asyncio.sleep(0.01)  # like a real consumer (push_data awaits), workers keep running
    assert outcomes == {"lever:a": "completed", "lever:b": "completed"}


# ---------- run(): SUMMARY.perCompany ----------
async def test_main_splits_max_items_evenly(source):
    p = Platform()
    await go(p, "abcdefgh", 8)
    assert [r["count"] for r in per_company(p).values()] == [1] * 8
    assert len(p.dataset) == 8


async def test_summary_lists_every_company_with_a_reason(source):
    source.update(few=2, nada=0, boom="boom")
    p = Platform()
    await go(p, ["few", "nada", "boom", "big1", "big2"], 12)
    pc = per_company(p)
    assert list(pc) == ["lever:few", "lever:nada", "lever:boom", "lever:big1", "lever:big2"]
    assert pc["lever:few"] == {"count": 2, "reason": "completed"}
    assert pc["lever:nada"] == {"count": 0, "reason": "no_matches"}
    assert pc["lever:boom"] == {"count": 0, "reason": "error"}
    assert pc["lever:big1"] == {"count": 5, "reason": "cap_reached"}
    assert pc["lever:big2"] == {"count": 5, "reason": "cap_reached"}
    s = p.kv["SUMMARY"]
    assert s["stoppedReason"] == "maxItems" and list(s["failedCompanies"]) == ["lever:boom"]
    assert s["perCompanyCounts"] == {"lever:few": 2, "lever:big1": 5, "lever:big2": 5}


async def test_error_after_some_jobs_keeps_count_and_reason_error(source):
    source.update(late=("fail_after", 3))
    p = Platform()
    await go(p, ["late", "big"], 100)
    assert per_company(p)["lever:late"] == {"count": 3, "reason": "error"}
    assert per_company(p)["lever:big"]["reason"] == "cap_reached"


async def test_all_companies_complete_with_max_items_to_spare(source):
    source.update(a=2, b=0)
    p = Platform()
    await go(p, ["a", "b"], 50)
    assert per_company(p) == {"lever:a": {"count": 2, "reason": "completed"},
                              "lever:b": {"count": 0, "reason": "no_matches"}}
    assert p.kv["SUMMARY"]["stoppedReason"] == "completed"


async def test_zero_job_company_is_listed_as_cap_reached_when_max_items_is_smaller(source):
    p = Platform()
    await go(p, "abcde", 2)
    pc = per_company(p)
    assert list(pc) == [f"lever:{c}" for c in "abcde"]
    assert sum(r["count"] for r in pc.values()) == 2
    assert {r["reason"] for r in pc.values()} == {"cap_reached"}
    assert any(r["count"] == 0 for r in pc.values())


async def test_budget_stop_reason(source, monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    source.update(nada=0)
    p = Platform(prices=dict(PRICES), max_total=D("0.01") + D("0.003") * 6)  # room for 6 jobs
    await go(p, ["nada", "big1", "big2"], 100)
    pc = per_company(p)
    assert p.kv["SUMMARY"]["stoppedReason"] == "budget"
    assert pc["lever:nada"] == {"count": 0, "reason": "no_matches"}
    assert pc["lever:big1"] == {"count": 3, "reason": "budget"}
    assert pc["lever:big2"] == {"count": 3, "reason": "budget"}
    assert p.charged["job-scraped"] == 6


async def test_budget_exhausted_before_start_marks_every_company_budget(source, monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("0.01"))  # start fee only
    await go(p, ["a", "b"], 10)
    assert per_company(p) == {"lever:a": {"count": 0, "reason": "budget"},
                              "lever:b": {"count": 0, "reason": "budget"}}


# ---------- restarts ----------
async def test_restart_keeps_fairness_and_total_cap(source):
    p = Platform()
    p.crash_on_push = 7  # 6 delivered (2 each), state not yet persisted: rebuilt from the dataset
    with pytest.raises(RuntimeError):
        await go(p, "abc", 12)
    assert len(p.dataset) == 6
    p.crash_on_push = None
    await go(p, "abc", 12)
    s = p.kv["SUMMARY"]
    assert len(p.dataset) == 12 and s["jobsPushed"] == 12 and s["jobsPushedThisRun"] == 6
    assert s["perCompanyCounts"] == {"lever:a": 4, "lever:b": 4, "lever:c": 4}
    assert [r["reason"] for r in s["perCompany"].values()] == ["cap_reached"] * 3
    ids = [j["jobId"] for j in p.dataset]
    assert len(ids) == len(set(ids))


async def test_restart_rebalances_an_uneven_earlier_split(source):
    p = Platform()
    rows = [("a", i) for i in range(5)] + [("b", 0)]
    p.dataset.extend({"atsSource": "lever", "companySlug": s, "jobId": f"{s}-{i}"} for s, i in rows)
    p.kv["JOBFEED_STATE"] = {
        "seen": [f"lever|{s}|{s}-{i}" for s, i in rows], "pushed": 6, "startCharged": False,
        "perCompany": {"lever:a": 5, "lever:b": 1}, "failed": {}, "notes": {},
        "startChargeAttempted": False}
    await go(p, "abc", 9)
    s = p.kv["SUMMARY"]
    assert s["perCompanyCounts"] == {"lever:a": 5, "lever:b": 2, "lever:c": 2}
    assert s["jobsPushed"] == 9 and s["jobsPushedThisRun"] == 3


async def test_restart_with_cap_already_reached_still_lists_every_company(source):
    p = Platform()
    rows = [("a", 0), ("a", 1)]
    p.dataset.extend({"atsSource": "lever", "companySlug": s, "jobId": f"{s}-{i}"} for s, i in rows)
    p.kv["JOBFEED_STATE"] = {
        "seen": [f"lever|{s}|{s}-{i}" for s, i in rows], "pushed": 2, "startCharged": False,
        "perCompany": {"lever:a": 2}, "failed": {}, "notes": {}, "startChargeAttempted": False}
    await go(p, "ab", 2)
    assert per_company(p) == {"lever:a": {"count": 2, "reason": "cap_reached"},
                              "lever:b": {"count": 0, "reason": "cap_reached"}}
    assert p.kv["SUMMARY"]["stoppedReason"] == "maxItems"


# ---------- review 6 ----------
def wd_refs():
    return [CompanyRef("workday", "acme", wd="wd5", site="Ext"),
            CompanyRef("workday", "acme", wd="wd5", site="Careers")]


@pytest.fixture
def overlapping_sites(monkeypatch):
    """Two Workday sites of one tenant, both exposing job ids 0-2 (same job keys)."""
    async def fake(client, ref, flt, include_description=True, report=None):
        for i in range(3):
            yield Job(title=f"Job {i}", company="Acme", location="X", remote=False, department=None,
                      postedDate=None, applyUrl=None, atsSource="workday", descriptionText=None,
                      jobId=str(i), companySlug=ref.slug)
            await asyncio.sleep(0)

    monkeypatch.setattr(pl, "fetch_company", fake)
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)


async def test_overlapping_sites_yield_unique_jobs_only(overlapping_sites):
    out = await collect(wd_refs(), max_items=4)
    ids = [j.jobId for j in out]
    assert sorted(ids) == ["0", "1", "2"]  # all unique rows; duplicates did not eat the slots


async def test_stale_duplicate_head_does_not_consume_share(overlapping_sites):
    # maxItems=2 -> share 1 each; the duplicate head of the second site must be dropped,
    # and the second site must still get to deliver its next, new job
    out = await collect(wd_refs(), max_items=2)
    assert sorted(j.jobId for j in out) == ["0", "1"]


async def test_overlapping_sites_are_not_charged_twice(overlapping_sites, monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    p = Platform(prices=dict(PRICES), max_total=D("10"))
    a = FakeActor(p, {"companies": ["workday:acme.wd5/Ext", "workday:acme.wd5/Careers"],
                      "maxItems": 4, "includeDescription": False})
    await run(a)
    ids = [r["jobId"] for r in p.dataset]
    assert sorted(ids) == ["0", "1", "2"]
    assert p.charged["job-scraped"] == 3


async def test_observed_error_is_reported_even_with_buffered_surplus(source):
    source.update(bad=("fail_after", 3))
    outcomes: dict[str, str] = {}
    out = []
    async for j in iter_jobs(None, refs_of("bad", "good"), JobFilter(), max_items=4,
                             outcomes=outcomes):
        out.append(j)
        await asyncio.sleep(0.01)  # like a real consumer: the worker fails while surplus is buffered
    t = tally(out)
    assert t == {"bad": 2, "good": 2}
    assert outcomes["lever:bad"] == "error" and outcomes["lever:good"] == "stopped"


async def test_summary_reports_error_with_delivered_count_despite_surplus(source, monkeypatch):
    source.update(bad=("fail_after", 3))
    real_push = FakeActor.push_data

    async def slow_push(self, *a, **kw):  # a real push awaits the platform; workers keep running
        await asyncio.sleep(0.01)
        return await real_push(self, *a, **kw)

    monkeypatch.setattr(FakeActor, "push_data", slow_push)
    p = Platform()
    await go(p, ["bad", "good"], 4)
    s = p.kv["SUMMARY"]
    assert s["perCompany"]["lever:bad"] == {"count": 2, "reason": "error"}
    assert "lever:bad" in s["failedCompanies"]
