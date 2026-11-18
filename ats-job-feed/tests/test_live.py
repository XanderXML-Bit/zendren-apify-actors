"""Live-network tests against real public career sites.

Run with:  pytest tests/test_live.py
Skip them offline with:  pytest -m "not live"
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.ats import Job, JobFilter, RateLimitedClient, parse_company
from src.pipeline import fetch_company

pytestmark = pytest.mark.live

COMPANIES = {
    "greenhouse": ["stripe", "airbnb", "discord"],
    "lever": ["spotify", "palantir", "weride"],
    "ashby": ["ramp", "notion", "linear"],
    "workday": [
        "nvidia.wd5/NVIDIAExternalCareerSite",
        "salesforce.wd12/External_Career_Site",
        "adobe.wd5/external_experienced",
    ],
}
CASES = [(ats, ref) for ats, refs in COMPANIES.items() for ref in refs]
REQUIRED = ["title", "company", "location", "remote", "department", "postedDate",
            "applyUrl", "atsSource", "descriptionText", "jobId", "salary"]


@pytest.fixture
async def client():
    async with RateLimitedClient() as c:
        yield c


async def collect(client, ref_str, flt=None, limit=5, include_description=True):
    ref = parse_company(ref_str)
    out = []
    gen = fetch_company(client, ref, flt or JobFilter(), include_description)
    try:
        async for job in gen:
            out.append(job)
            if len(out) >= limit:
                break
    finally:
        await gen.aclose()
    return ref, out


def assert_valid(job: Job, ats: str):
    d = job.to_dict()
    for key in REQUIRED:
        assert key in d, key
    assert job.atsSource == ats
    assert job.title and job.company and job.jobId
    assert isinstance(job.remote, bool)
    assert job.applyUrl and job.applyUrl.startswith("http")
    assert job.descriptionText and len(job.descriptionText) > 40
    assert "<div" not in job.descriptionText and "</p>" not in job.descriptionText
    assert "&lt;" not in job.descriptionText
    if job.postedDate:
        datetime.fromisoformat(job.postedDate.replace("Z", "+00:00"))


@pytest.mark.parametrize("ats,ref", CASES, ids=[f"{a}:{r}" for a, r in CASES])
async def test_company_returns_valid_records(client, ats, ref):
    _, jobs = await collect(client, f"{ats}:{ref}", limit=5)
    assert len(jobs) >= 1, f"{ats}:{ref} returned no jobs"
    for job in jobs:
        assert_valid(job, ats)
    assert any(j.postedDate for j in jobs), "no posted date parsed"


@pytest.mark.parametrize("url,ats", [
    ("https://boards.greenhouse.io/stripe", "greenhouse"),
    ("https://job-boards.greenhouse.io/discord", "greenhouse"),
    ("https://jobs.lever.co/spotify", "lever"),
    ("https://jobs.ashbyhq.com/ramp", "ashby"),
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "workday"),
])
async def test_url_inputs_work_end_to_end(client, url, ats):
    _, jobs = await collect(client, url, limit=2)
    assert jobs and all(j.atsSource == ats for j in jobs)


async def test_include_description_false(client):
    for ref in ("ashby:ramp", "workday:nvidia.wd5/NVIDIAExternalCareerSite"):
        _, jobs = await collect(client, ref, limit=3, include_description=False)
        assert jobs and all(j.descriptionText is None for j in jobs)


async def test_keyword_filter_live(client):
    _, jobs = await collect(client, "greenhouse:stripe", JobFilter(keyword="engineer"), limit=5)
    assert jobs and all("engineer" in (j.title + (j.department or "")).lower() for j in jobs)


async def test_workday_keyword_and_pagination(client):
    # >20 results forces a second page through the offset logic
    _, jobs = await collect(client, "workday:nvidia.wd5/NVIDIAExternalCareerSite",
                            JobFilter(keyword="software"), limit=30, include_description=False)
    assert len(jobs) == 30
    assert len({j.jobId for j in jobs}) == 30
    assert all("software" in j.title.lower() for j in jobs)


async def test_remote_only_live(client):
    _, jobs = await collect(client, "ashby:ramp", JobFilter(remote_only=True), limit=3)
    assert jobs and all(j.remote for j in jobs)


async def test_location_filter_live(client):
    _, jobs = await collect(client, "lever:spotify", JobFilter(location="stockholm"), limit=3)
    assert jobs
    assert all("stockholm" in " ".join([j.location or "", *j.locations]).lower() for j in jobs)


async def test_unknown_board_raises_http_error(client):
    from src.ats import HttpStatusError
    with pytest.raises(HttpStatusError):
        await collect(client, "greenhouse:this-company-does-not-exist-zzz")
