"""Offline regression tests for review findings on the incremental mode (P1 + three P2 findings)."""

import logging
from decimal import Decimal as D

import pytest

import src.incremental as inc
import src.main as main_mod
import src.pipeline as pl
from src.ats import CompanyRef, Job, workday
from src.ats.models import AtsError
from src.incremental import MAX_ENTRIES, STORE_NAME, SeenState, record_key
from src.main import run
from tests.fakes import FakeActor, Platform

OLD = {"actor-start": D("0.01"), "job-scraped": D("0.003")}
NEW = {"apify-actor-start": D("0.00005"), "job-scraped": D("0.0015")}
INP = {"companies": ["lever:acme"], "maxItems": 10, "includeDescription": False, "onlyNewJobs": True,
       "stateKey": "feed"}


def make_job(ref, i):
    return Job(title=f"Job {i}", company="Acme", location="X", remote=False, department=None,
               postedDate=None, applyUrl=None, atsSource=ref.ats, descriptionText=None,
               jobId=str(i), companySlug=ref.slug)


def next_run(prev: Platform) -> Platform:
    return Platform(prices=dict(prev.prices), max_total=prev.max_total, named_kv=prev.named_kv)


async def go(p, inp=None):
    a = FakeActor(p, {**INP, **(inp or {})})
    await run(a)
    return a


def state_jobs(p):
    store = p.named_kv[STORE_NAME]
    return store[record_key("feed")]["jobs"]


@pytest.fixture(autouse=True)
def charging(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    monkeypatch.setattr(inc, "today", lambda: 20000)


@pytest.fixture
def lever_source(monkeypatch):
    async def fake(client, ref, flt, include_description=True, report=None):
        for i in range(60):
            yield make_job(ref, i)

    monkeypatch.setattr(pl, "fetch_company", fake)


# ---------- P1: an unconfirmed charge never enters the named state ----------
async def test_unconfirmed_charge_row_is_not_remembered_in_named_state(lever_source):
    p = Platform(prices=dict(OLD), max_total=D("10"))
    p.fail_charge_calls = {7}  # call 1 is the start fee; the charge of job 5 (call 7) fails after the write
    with pytest.raises(RuntimeError):
        await go(p)
    assert len(p.dataset) == 6 and p.charged["job-scraped"] == 5
    assert "lever|acme|5" not in state_jobs(p)  # not marked by the crashed process either

    await go(p)  # restart of the same run: the row is delivered, its charge stays unconfirmed
    assert list(p.kv["SUMMARY"]["unbilledJobs"]) == ["lever|acme|5"]
    jobs = state_jobs(p)
    assert "lever|acme|5" not in jobs
    assert {f"lever|acme|{i}" for i in (0, 1, 2, 3, 4, 6, 7, 8, 9)} <= set(jobs)

    await go(p, {"maxItems": 12})  # a second restart keeps the row out of the state
    assert "lever|acme|5" not in state_jobs(p)

    later = next_run(p)  # a later run may deliver the unconfirmed job again (accepted trade-off)
    await go(later)
    assert "5" in [j["jobId"] for j in later.dataset]
    assert later.charged["job-scraped"] == len(later.dataset)
    assert "lever|acme|5" in state_jobs(later)  # delivered and confirmed this time


async def test_recovered_confirmed_rows_are_still_remembered(lever_source):
    """A clean crash (before the write) loses nothing: all earlier, confirmed rows stay remembered."""
    p = Platform(prices=dict(NEW), max_total=D("10"))
    p.crash_on_push = 7
    with pytest.raises(RuntimeError):
        await go(p)
    p.crash_on_push = None
    await go(p)
    assert p.kv["SUMMARY"]["unbilledJobs"] == {}
    assert len(state_jobs(p)) == 10 == p.charged["job-scraped"]


async def test_without_charging_recovered_rows_are_remembered(monkeypatch, lever_source):
    monkeypatch.delenv("CHARGING_ENABLED")
    p = Platform()
    p.crash_on_push = 5
    with pytest.raises(RuntimeError):
        await go(p)
    p.named_kv.clear()  # a hard kill: the named state was never saved, only the dataset survived
    p.crash_on_push = None
    await go(p)
    assert len(p.dataset) == 10 and len(state_jobs(p)) == 10


# ---------- P2: Workday identity does not depend on the detail request ----------
class FakeWorkday:
    def __init__(self, postings, *, detail_ok=True):
        self.postings = postings
        self.detail_ok = detail_ok

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post_json(self, url, payload):
        off, lim = payload["offset"], payload["limit"]
        return {"total": len(self.postings) if off == 0 else 0, "jobPostings": self.postings[off:off + lim]}

    async def get_json(self, url, params=None):
        if not self.detail_ok:
            raise AtsError("detail endpoint unavailable")
        path = url.split("/Site", 1)[1]
        req = path.rsplit("_", 1)[-1]
        return {"jobPostingInfo": {"jobReqId": req, "jobDescription": "<p>d</p>", "title": "Engineer"}}


WD_POSTINGS = [{"title": "Engineer", "externalPath": "/job/London/Engineer_R123", "locationsText": "London",
                "bulletFields": ["Posted Today"]}]
WD_INP = {"companies": ["workday:acme.wd5/Site"], "maxItems": 10, "onlyNewJobs": True, "stateKey": "feed",
          "includeDescription": True}


def test_workday_job_id_is_the_same_with_and_without_detail():
    ref = CompanyRef("workday", "acme", wd="wd5", site="Site")
    p = WD_POSTINGS[0]
    detail = {"jobPostingInfo": {"jobReqId": "R123", "jobDescription": "<p>d</p>"}}
    ids = {workday.normalize(p, detail, ref, True).jobId, workday.normalize(p, None, ref, True).jobId,
           workday.normalize(p, None, ref, False).jobId}
    assert ids == {"R123"}
    # no requisition id anywhere: the last path segment, consistently
    bare = {"title": "T", "externalPath": "/job/London/Engineer"}
    assert workday.normalize(bare, None, ref, False).jobId == "Engineer"
    # a list bullet that looks like a requisition id still wins
    assert workday.job_id_from_list({**p, "bulletFields": ["JR77"]}) == "JR77"


@pytest.mark.parametrize("first, second", [
    ({"includeDescription": True}, {"includeDescription": False}),   # descriptions on -> off
    ({"includeDescription": False}, {"includeDescription": True}),   # off -> on
])
async def test_workday_description_toggle_does_not_redeliver(monkeypatch, first, second):
    monkeypatch.setattr(main_mod, "RateLimitedClient", lambda: FakeWorkday(WD_POSTINGS))
    p1 = Platform(prices=dict(NEW), max_total=D("10"))
    await go(p1, {**WD_INP, **first})
    p2 = next_run(p1)
    await go(p2, {**WD_INP, **second})
    p3 = next_run(p2)
    await go(p3, {**WD_INP, **first})
    assert [j["jobId"] for j in p1.dataset] == ["R123"] and p2.dataset == [] and p3.dataset == []
    assert p2.charge_calls == [] and p3.charge_calls == []


async def test_workday_detail_success_failure_success_delivers_once(monkeypatch):
    p1 = Platform(prices=dict(NEW), max_total=D("10"))
    monkeypatch.setattr(main_mod, "RateLimitedClient", lambda: FakeWorkday(WD_POSTINGS, detail_ok=True))
    await go(p1, WD_INP)
    p2 = next_run(p1)
    monkeypatch.setattr(main_mod, "RateLimitedClient", lambda: FakeWorkday(WD_POSTINGS, detail_ok=False))
    await go(p2, WD_INP)  # detail requests fail: the list record is used
    assert p2.kv["SUMMARY"]["detailFailures"]
    p3 = next_run(p2)
    monkeypatch.setattr(main_mod, "RateLimitedClient", lambda: FakeWorkday(WD_POSTINGS, detail_ok=True))
    await go(p3, WD_INP)
    assert len(p1.dataset) == 1 and p2.dataset == [] and p3.dataset == []
    assert p1.charged["job-scraped"] + p2.charged["job-scraped"] + p3.charged["job-scraped"] == 1


# ---------- P2: distinct state keys never share a record ----------
def test_record_key_always_carries_a_hash_of_the_original():
    assert record_key("daily-eng").startswith("STATE-daily-eng-")
    # the reviewer's collision: a sanitized key vs a valid-looking key equal to its output
    sanitized = record_key("a b")
    valid_lookalike = record_key(sanitized[len("STATE-"):])
    assert sanitized != valid_lookalike
    assert record_key("a_b-" + sanitized.rsplit("-", 1)[1]) != sanitized
    keys = ["a b", "a_b", "a b ", "A B", "a/b", "a\\b", "é", "e", "", " "]
    assert len({record_key(k) for k in keys}) == len(keys)


async def test_state_keys_that_sanitize_alike_are_independent_feeds(lever_source):
    p1 = Platform(prices=dict(NEW), max_total=D("10"))
    await go(p1, {"stateKey": "a b"})
    suffix = record_key("a b").rsplit("-", 1)[1]
    p2 = next_run(p1)
    await go(p2, {"stateKey": "a_b-" + suffix})  # equals the *old* sanitized form of "a b"
    assert len(p2.dataset) == 10  # fresh feed, nothing skipped
    assert len(p2.named_kv[STORE_NAME]) == 2


# ---------- P2: the state is bounded ----------
def _store_with(p, n, *, key="STATE-x", base_day=19000):
    p.named_kv[STORE_NAME] = {key: {"v": 1, "jobs": {f"a|b|{i}": base_day + i // 1000 for i in range(n)}}}


async def _load(p, n_cap=None, **kw):
    kvs = await FakeActor(p, {}).open_key_value_store(name=STORE_NAME)
    st = SeenState(kvs, "STATE-x", day=20000, **({"max_entries": n_cap} if n_cap else {}), **kw)
    await st.load()
    return st


async def test_default_cap_is_enforced_on_load_at_the_boundary(caplog):
    assert MAX_ENTRIES == 200_000
    p = Platform()
    _store_with(p, MAX_ENTRIES, base_day=19950)
    st = await _load(p)
    assert len(st.jobs) == MAX_ENTRIES and st.evicted == 0  # exactly at the cap: nothing evicted
    p2 = Platform()
    _store_with(p2, MAX_ENTRIES + 1, base_day=19950)
    with caplog.at_level(logging.WARNING, logger="jobfeed"):
        st2 = await _load(p2)
    assert len(st2.jobs) == MAX_ENTRIES and st2.evicted == 1 and "a|b|0" not in st2.jobs  # oldest went
    assert "a|b|0" not in st2.known and any("evicted" in r.message for r in caplog.records)


async def test_eviction_drops_least_recently_seen_and_applies_on_save():
    p = Platform()
    p.named_kv[STORE_NAME] = {"STATE-x": {"v": 1, "jobs": {"old": 19990, "mid": 19995, "new": 19999}}}
    st = await _load(p, 3)
    assert st.evicted == 0
    st.touch("old")  # refreshed jobs are the most recent now
    st.mark("fresh")
    await st.save()
    saved = p.named_kv[STORE_NAME]["STATE-x"]["jobs"]
    assert set(saved) == {"old", "new", "fresh"} and st.evicted == 1  # "mid" was the least recent
    st.mark("fresh2")
    await st.save()
    assert len(p.named_kv[STORE_NAME]["STATE-x"]["jobs"]) == 3 and st.evicted == 2


async def test_refreshed_jobs_cannot_grow_the_state_past_the_cap():
    p = Platform()
    p.named_kv[STORE_NAME] = {}
    kvs = await FakeActor(p, {}).open_key_value_store(name=STORE_NAME)
    day, live = 20000, []
    for step in range(30):  # 5 new jobs per "day", everything listed is refreshed
        st = SeenState(kvs, "STATE-x", day=day + step, max_entries=50)
        await st.load()
        for k in live:
            st.touch(k)
        new = [f"a|b|{step}-{i}" for i in range(5)]
        for k in new:
            st.mark(k)
        live += new
        await st.save()
        assert len(p.named_kv[STORE_NAME]["STATE-x"]["jobs"]) <= 50


async def test_stateevicted_is_reported_in_summary(lever_source, monkeypatch):
    monkeypatch.setattr(inc.SeenState.__init__, "__kwdefaults__",
                        {"day": None, "retention": inc.RETENTION_DAYS, "max_entries": 6})
    p1 = Platform(prices=dict(NEW), max_total=D("10"))
    await go(p1, {"maxItems": 4})
    assert p1.kv["SUMMARY"]["stateEvicted"] == 0
    p2 = next_run(p1)
    await go(p2, {"maxItems": 4})  # 8 remembered > 6: two evicted on save
    assert p2.kv["SUMMARY"]["stateEvicted"] == 2 and len(state_jobs(p2)) == 6
    p3 = Platform(prices=dict(NEW), max_total=D("10"))
    await go(p3, {"onlyNewJobs": False})
    assert p3.kv["SUMMARY"]["stateEvicted"] is None
