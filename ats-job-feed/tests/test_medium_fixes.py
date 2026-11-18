"""Offline tests: Workday pagination/truncation/detail failures, filters, pipeline streaming."""

import asyncio

import pytest

from src.ats import CompanyRef, Job, JobFilter, RunReport, greenhouse, workday
from src.ats.models import AtsError
from src.pipeline import iter_jobs

REF = CompanyRef("workday", "acme", wd="wd5", site="Site")


class FakeWorkday:
    """Duck-typed RateLimitedClient serving a Workday tenant from memory."""

    def __init__(self, postings, *, fail_detail=False, detail=None):
        self.postings = postings
        self.fail_detail = fail_detail
        self.detail = detail or {}
        self.offsets = []
        self.detail_calls = 0

    async def post_json(self, url, payload):
        off, lim = payload["offset"], payload["limit"]
        self.offsets.append(off)
        # like the real API: total is only reported on the first page
        return {"total": len(self.postings) if off == 0 else 0,
                "jobPostings": self.postings[off:off + lim]}

    async def get_json(self, url, params=None):
        self.detail_calls += 1
        if self.fail_detail:
            raise AtsError("detail endpoint unavailable")
        path = url.split("/Site", 1)[1]
        return self.detail.get(path, {"jobPostingInfo": {"jobDescription": "<p>d</p>"}})


def posting(i, title="Engineer", loc="London, UK", **kw):
    return {"title": title, "externalPath": f"/job/p/{i}", "locationsText": loc,
            "bulletFields": [f"JR{i}"], **kw}


async def collect(client, flt=JobFilter(), include_description=False, report=None):
    return [j async for j in workday.fetch_jobs(client, REF, flt, include_description, report=report)]


# ---------- pagination / truncation ----------
async def test_workday_paginates_all_pages():
    c = FakeWorkday([posting(i) for i in range(45)])
    report = RunReport()
    jobs = await collect(c, report=report)
    assert len(jobs) == 45 and c.offsets == [0, 20, 40]
    assert report.truncated == {}


async def test_workday_truncation_is_reported(monkeypatch, caplog):
    monkeypatch.setattr(workday, "MAX_PAGES", 3)
    c = FakeWorkday([posting(i) for i in range(100)])
    report = RunReport()
    with caplog.at_level("WARNING"):
        jobs = await collect(c, report=report)
    assert len(jobs) == 60 and c.offsets == [0, 20, 40]
    assert REF.key in report.truncated and "of 100" in report.truncated[REF.key]
    assert "truncated" in caplog.text
    assert report.to_dict()["truncatedCompanies"]


async def test_workday_exact_fit_at_page_cap_is_not_truncated(monkeypatch):
    monkeypatch.setattr(workday, "MAX_PAGES", 3)
    report = RunReport()
    jobs = await collect(FakeWorkday([posting(i) for i in range(60)]), report=report)
    assert len(jobs) == 60 and report.truncated == {}


# ---------- detail failures (M9) ----------
async def test_detail_failure_does_not_drop_multi_location_job_and_is_counted():
    c = FakeWorkday([posting(1, loc="2 Locations"), posting(2, loc="Berlin, Germany")], fail_detail=True)
    report = RunReport()
    jobs = await collect(c, JobFilter(location="London"), include_description=False, report=report)
    # unknown locations are not a mismatch; the concrete "Berlin" list location is
    assert [j.jobId for j in jobs] == ["JR1"]
    assert report.detail_failures[REF.key] == 1
    assert report.unverified[REF.key] == 1
    assert report.to_dict()["detailFailures"] == {REF.key: 1}


async def test_detail_failure_with_descriptions_keeps_job_without_description():
    c = FakeWorkday([posting(1), posting(2, loc="2 Locations")], fail_detail=True)
    report = RunReport()
    jobs = await collect(c, JobFilter(location="London"), include_description=True, report=report)
    assert [j.jobId for j in jobs] == ["JR1", "JR2"]
    assert all(not j.descriptionText for j in jobs)
    assert report.detail_failures[REF.key] == 2
    assert report.unverified[REF.key] == 1  # only JR2's location was unverifiable


async def test_detail_success_filters_by_real_locations():
    detail = {"/job/p/1": {"jobPostingInfo": {"location": "Paris, France", "additionalLocations": ["Rome"]}}}
    c = FakeWorkday([posting(1, loc="2 Locations")], detail=detail)
    report = RunReport()
    assert await collect(c, JobFilter(location="London"), report=report) == []
    assert report.detail_failures == {} and report.unverified == {}


async def test_remote_only_with_failed_detail_is_unknown_not_mismatch():
    c = FakeWorkday([posting(1, loc="Austin, TX")], fail_detail=True)
    report = RunReport()
    jobs = await collect(c, JobFilter(remote_only=True), report=report)
    assert len(jobs) == 1 and report.unverified[REF.key] == 1


# ---------- remote detection (M8) ----------
def _detail(**info):
    return {"jobPostingInfo": info}


def test_workday_additional_location_counts_for_remote():
    j = workday.normalize(posting(1, loc="London"), _detail(location="London",
                          additionalLocations=["Remote, UK"]), REF, False)
    assert j.remote
    assert JobFilter(remote_only=True).match(j)


def test_workday_explicit_hybrid_metadata_wins_over_location_text():
    j = workday.normalize(posting(1), _detail(location="London", remoteType="Hybrid",
                          additionalLocations=["Remote, UK"]), REF, False)
    assert not j.remote


def test_workday_remote_type_remote():
    j = workday.normalize(posting(1), _detail(location="London", remoteType="Remote"), REF, False)
    assert j.remote


# ---------- location matching (M6) ----------
def _job(**kw):
    base = dict(title="Manager", company="X", location="London, UK", remote=False, department=None,
                postedDate=None, applyUrl=None, atsSource="lever", descriptionText=None, jobId="1")
    base.update(kw)
    return Job(**base)


def test_location_words_must_match_the_same_location():
    j = _job(locations=["London, UK", "Berlin, Germany"])
    assert not JobFilter(location="London Germany").match(j)
    assert JobFilter(location="Berlin Germany").match(j)
    assert JobFilter(location="London UK").match(j)
    assert JobFilter(location="Paris, Berlin").match(j)  # alternatives still OR
    assert JobFilter(location="London Germany, Berlin").match(j)


# ---------- Greenhouse departments (M7) ----------
RAW = {"id": 1, "title": "Manager", "location": {"name": "NYC"},
       "departments": [{"name": "Sales"}, {"name": "Engineering"}]}


def test_greenhouse_keeps_all_departments():
    j = greenhouse.normalize(RAW, CompanyRef("greenhouse", "acme"), False)
    assert j.department == "Sales"  # first, unchanged for existing consumers
    assert j.departments == ["Sales", "Engineering"]
    assert j.to_dict()["departments"] == ["Sales", "Engineering"]
    assert JobFilter(keyword="engineering").match(j)
    assert JobFilter(keyword="sales").match(j)
    assert not JobFilter(keyword="legal").match(j)


def test_other_ats_get_single_item_departments():
    assert _job(department="Eng").departments == ["Eng"]
    assert _job(department=None).departments == []


# ---------- pipeline streaming (M10) ----------
async def test_slow_company_does_not_block_ready_ones(monkeypatch):
    import src.pipeline as pl
    release = asyncio.Event()

    async def fake(client, ref, flt, include_description=True, report=None):
        if ref.slug == "slow":
            await release.wait()
        for i in range(3):
            yield _job(jobId=f"{ref.slug}-{i}", atsSource="lever", companySlug=ref.slug)

    monkeypatch.setattr(pl, "fetch_company", fake)
    refs = [CompanyRef("lever", "slow"), CompanyRef("lever", "fast")]
    out = await asyncio.wait_for(
        _collect(iter_jobs(None, refs, JobFilter(), max_items=1)), timeout=2)
    assert [j.companySlug for j in out] == ["fast"]  # slow one is still blocked


async def test_results_stream_while_slow_company_is_pending(monkeypatch):
    import src.pipeline as pl
    release = asyncio.Event()

    async def fake(client, ref, flt, include_description=True, report=None):
        if ref.slug == "slow":
            await release.wait()
        for i in range(2):
            yield _job(jobId=f"{ref.slug}-{i}", atsSource="lever", companySlug=ref.slug)

    monkeypatch.setattr(pl, "fetch_company", fake)
    refs = [CompanyRef("lever", "slow"), CompanyRef("lever", "fast")]
    got = []
    async for j in iter_jobs(None, refs, JobFilter(), max_items=4):
        got.append(j.companySlug)
        if len(got) == 2:
            assert got == ["fast", "fast"]  # delivered before the slow source returned anything
            release.set()
    assert sorted(got) == ["fast", "fast", "slow", "slow"]


async def test_pipeline_skips_seen_keys_and_reports_company_errors(monkeypatch):
    import src.pipeline as pl

    async def fake(client, ref, flt, include_description=True, report=None):
        if ref.slug == "boom":
            raise AtsError("kaput")
        for i in range(5):
            yield _job(jobId=str(i), atsSource="lever", companySlug=ref.slug)

    monkeypatch.setattr(pl, "fetch_company", fake)
    errors, seen = [], {"lever|a|0", "lever|a|1"}
    refs = [CompanyRef("lever", "a"), CompanyRef("lever", "boom")]
    out = await _collect(iter_jobs(None, refs, JobFilter(), max_items=10, seen=seen,
                                   on_error=lambda r, e: errors.append(r.slug)))
    assert [j.jobId for j in out] == ["2", "3", "4"] and errors == ["boom"]
    assert "lever|a|4" in seen


async def _collect(agen):
    return [x async for x in agen]
