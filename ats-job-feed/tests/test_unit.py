"""Offline tests: URL parsing, filters, text/date helpers, HTTP retry logic."""

from datetime import date

import httpx
import pytest

from src.ats import CompanyRef, Job, JobFilter, RateLimitedClient, parse_company
from src.ats.filters import split_alternatives
from src.ats.http import parse_retry_after
from src.ats.models import AtsError, HttpStatusError
from src.ats.text import html_to_text
from src.ats.workday import parse_posted_on
from src.ats import greenhouse, ashby, lever, workday
from src.main import _entries
from src.pipeline import iter_jobs


# ---------- URL / ref parsing ----------
@pytest.mark.parametrize("entry,expected", [
    ("greenhouse:stripe", CompanyRef("greenhouse", "stripe")),
    ("Greenhouse: stripe ", CompanyRef("greenhouse", "stripe")),
    ("https://boards.greenhouse.io/airbnb", CompanyRef("greenhouse", "airbnb")),
    ("https://boards.greenhouse.io/airbnb/jobs/123", CompanyRef("greenhouse", "airbnb")),
    ("https://job-boards.greenhouse.io/discord", CompanyRef("greenhouse", "discord")),
    ("job-boards.eu.greenhouse.io/acme", CompanyRef("greenhouse", "acme")),
    ("https://boards.greenhouse.io/embed/job_board?for=figma", CompanyRef("greenhouse", "figma")),
    ("lever:spotify", CompanyRef("lever", "spotify")),
    ("https://jobs.lever.co/palantir/abc-123", CompanyRef("lever", "palantir")),
    ("https://jobs.eu.lever.co/acme", CompanyRef("lever", "acme", region="eu")),
    ("lever:eu/acme", CompanyRef("lever", "acme", region="eu")),
    ("ashby:ramp", CompanyRef("ashby", "ramp")),
    ("https://jobs.ashbyhq.com/notion", CompanyRef("ashby", "notion")),
    ("https://jobs.ashbyhq.com/linear/1234-5678", CompanyRef("ashby", "linear")),
    ("workday:nvidia.wd5/NVIDIAExternalCareerSite",
     CompanyRef("workday", "nvidia", wd="wd5", site="NVIDIAExternalCareerSite")),
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
     CompanyRef("workday", "nvidia", wd="wd5", site="NVIDIAExternalCareerSite")),
    ("https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/Foo/Bar_JR1",
     CompanyRef("workday", "salesforce", wd="wd12", site="External_Career_Site")),
])
def test_parse_company(entry, expected):
    assert parse_company(entry) == expected


@pytest.mark.parametrize("bad", [
    "", "stripe", "https://example.com/careers", "workday:nvidia",
    "https://boards.greenhouse.io/", "greenhouse:", "https://jobs.lever.co/",
    "https://nvidia.myworkdayjobs.com/Site",
])
def test_parse_company_rejects(bad):
    with pytest.raises(ValueError):
        parse_company(bad)


def test_company_key():
    assert parse_company("workday:nvidia.wd5/X").key == "workday:nvidia.wd5/X"
    assert parse_company("lever:a").key == "lever:a"


def test_entries_accepts_strings_commas_and_objects():
    raw = ["lever:a, ashby:b", {"url": "https://jobs.lever.co/c"}, "", None]
    assert _entries(raw) == ["lever:a", "ashby:b", "https://jobs.lever.co/c"]


# ---------- filters ----------
def _job(**kw):
    base = dict(title="Senior Data Engineer", company="X", location="London, UK", remote=False,
                department="Engineering", postedDate=None, applyUrl=None, atsSource="lever",
                descriptionText=None, jobId="1")
    base.update(kw)
    return Job(**base)


def test_split_alternatives():
    assert split_alternatives("data engineer, ML | ops") == [["data", "engineer"], ["ml"], ["ops"]]
    assert split_alternatives("  ") == []


def test_keyword_filter():
    j = _job()
    assert JobFilter(keyword="data engineer").match(j)
    assert JobFilter(keyword="ENGINEER").match(j)
    assert JobFilter(keyword="engineering").match(j)  # department
    assert JobFilter(keyword="designer, data").match(j)  # OR
    assert not JobFilter(keyword="data scientist").match(j)  # AND
    assert JobFilter().match(j)


def test_location_filter_uses_all_locations():
    j = _job(locations=["London, UK", "Remote (US)"])
    assert JobFilter(location="london").match(j)
    assert JobFilter(location="remote (us)").match(j)
    assert JobFilter(location="berlin, london").match(j)
    assert not JobFilter(location="berlin").match(j)


def test_remote_only():
    assert not JobFilter(remote_only=True).match(_job(remote=False))
    assert JobFilter(remote_only=True).match(_job(remote=True))


# ---------- text / dates ----------
def test_html_to_text():
    out = html_to_text("<div><p>Hello&nbsp;<b>world</b> &amp; co</p><ul><li>One</li><li>Two</li></ul>"
                       "<script>x()</script></div>")
    assert out == "Hello world & co\n\n- One\n- Two"
    assert "<" not in out and "script" not in out and "x()" not in out
    assert "- One" in out and "- Two" in out and "Hello world & co" in out


def test_html_to_text_greenhouse_escaped():
    assert html_to_text("&lt;p&gt;Join &amp;amp; build&lt;/p&gt;") == "Join & build"


def test_html_to_text_empty():
    assert html_to_text(None) == "" and html_to_text("") == ""


@pytest.mark.parametrize("text,expected", [
    ("Posted Today", "2026-10-16"),
    ("Posted Yesterday", "2026-10-15"),
    ("Posted 3 Days Ago", "2026-10-13"),
    ("Posted 1 Day Ago", "2026-10-15"),
    ("Posted 2 Weeks Ago", "2026-10-02"),
    ("Posted 30+ Days Ago", None),
    ("something else", None),
    (None, None),
])
def test_parse_posted_on(text, expected):
    assert parse_posted_on(text, today=date(2026, 10, 16)) == expected


def test_parse_retry_after():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=1792567650.0) is not None


# ---------- normalizers on canned payloads ----------
def test_greenhouse_normalize():
    ref = CompanyRef("greenhouse", "acme")
    raw = {"id": 42, "title": " Engineer ", "company_name": "Acme", "absolute_url": "https://x/42",
           "location": {"name": "Remote (US)"}, "first_published": "2026-01-02T03:04:05-04:00",
           "departments": [{"name": "Eng"}], "offices": [{"name": "NYC"}],
           "content": "&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;",
           "pay_input_ranges": [{"min_cents": 10000000, "max_cents": 15000000, "currency_type": "USD"}]}
    j = greenhouse.normalize(raw, ref, True)
    assert (j.title, j.company, j.remote, j.department, j.jobId) == ("Engineer", "Acme", True, "Eng", "42")
    assert j.descriptionText == "Hi & bye"
    assert j.salary["min"] == 100000 and j.salary["currency"] == "USD"
    assert greenhouse.normalize(raw, ref, False).descriptionText is None


def test_lever_normalize():
    ref = CompanyRef("lever", "acme-co")
    raw = {"id": "abc", "text": "Dev", "createdAt": 1790000000000, "workplaceType": "remote",
           "categories": {"location": "Berlin", "department": "R&D", "commitment": "Full-time"},
           "description": "<div>Intro</div>", "lists": [{"text": "Do", "content": "<li>A</li>"}],
           "additional": "<div>EOE</div>", "hostedUrl": "https://jobs.lever.co/acme-co/abc",
           "applyUrl": "https://jobs.lever.co/acme-co/abc/apply"}
    j = lever.normalize(raw, ref, True)
    assert j.company == "Acme Co" and j.remote and j.postedDate.startswith("2026-")
    assert "Intro" in j.descriptionText and "- A" in j.descriptionText and "EOE" in j.descriptionText
    assert j.applyUrl.endswith("/apply") and j.employmentType == "Full-time"


def test_ashby_normalize_hybrid_is_not_remote():
    ref = CompanyRef("ashby", "ramp")
    raw = {"id": "1", "title": "T", "location": "NYC", "isRemote": True, "workplaceType": "Hybrid",
           "secondaryLocations": [{"location": "Remote (US)"}], "publishedAt": "2026-04-07T17:12:35.753+00:00",
           "jobUrl": "https://jobs.ashbyhq.com/ramp/1", "applyUrl": "https://jobs.ashbyhq.com/ramp/1/application",
           "descriptionPlain": "Plain", "department": "Eng",
           "compensation": {"compensationTierSummary": "$1K", "summaryComponents": [
               {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD",
                "minValue": 1000, "maxValue": 2000}]}}
    j = ashby.normalize(raw, ref, True)
    assert not j.remote and j.locations == ["NYC", "Remote (US)"]
    assert j.salary == {"min": 1000, "max": 2000, "currency": "USD", "interval": "1 YEAR", "summary": "$1K"}


def test_workday_normalize_list_only():
    ref = CompanyRef("workday", "nvidia", wd="wd5", site="Site")
    posting = {"title": "SWE", "externalPath": "/job/US-CA/SWE_JR123", "locationsText": "US, CA",
               "postedOn": "Posted Today", "bulletFields": ["JR123"]}
    j = workday.normalize(posting, None, ref, False)
    assert j.jobId == "JR123" and j.applyUrl == "https://nvidia.wd5.myworkdayjobs.com/Site/job/US-CA/SWE_JR123"
    assert j.postedDate is not None and j.descriptionText is None and not j.remote


def test_workday_normalize_with_detail():
    ref = CompanyRef("workday", "nvidia", wd="wd5", site="Site")
    posting = {"title": "SWE", "externalPath": "/job/x/SWE_JR9", "locationsText": "2 Locations"}
    detail = {"jobPostingInfo": {"title": "SWE", "jobDescription": "<p>Do things</p>", "location": "Remote, US",
                                 "additionalLocations": ["Austin, TX"], "startDate": "2026-10-01",
                                 "jobReqId": "JR9", "remoteType": "Remote", "externalUrl": "https://u/JR9"}}
    j = workday.normalize(posting, detail, ref, True)
    assert j.remote and j.postedDate == "2026-10-01" and j.descriptionText == "Do things"
    assert j.locations == ["Remote, US", "Austin, TX"] and j.applyUrl == "https://u/JR9"


# ---------- HTTP client (mock transport) ----------
def _client(handler, **kw):
    return RateLimitedClient(transport=httpx.MockTransport(handler), delay=0, backoff_base=0.01, **kw)


async def test_retries_on_429_then_succeeds_and_honours_retry_after(monkeypatch):
    calls, sleeps = [], []
    real_sleep = __import__("asyncio").sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)

    monkeypatch.setattr("src.ats.http.asyncio.sleep", fake_sleep)

    def handler(req):
        calls.append(req.headers["user-agent"])
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as c:
        assert await c.get_json("https://example.com/x") == {"ok": True}
    assert len(calls) == 3 and "ApifyJobFeed" in calls[0]
    assert any(s >= 2.9 for s in sleeps)  # Retry-After honoured (via the host cool-down)


async def test_non_retryable_status_raises_immediately():
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(404)

    async with _client(handler) as c:
        with pytest.raises(HttpStatusError) as ei:
            await c.get_json("https://example.com/x")
    assert ei.value.status == 404 and len(n) == 1


async def test_gives_up_after_max_retries():
    async with _client(lambda r: httpx.Response(500), max_retries=2) as c:
        with pytest.raises(AtsError):
            await c.get_json("https://example.com/x")


async def test_per_host_concurrency_limit():
    import asyncio
    active = peak = 0

    async def slow(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json={})

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await slow(request)

    async with RateLimitedClient(transport=T(), per_host_concurrency=2, delay=0) as c:
        await asyncio.gather(*(c.get_json(f"https://h.example/{i}") for i in range(8)))
    assert peak == 2


# ---------- pipeline ----------
async def test_pipeline_round_robin_error_isolation_and_limit(monkeypatch):
    import src.pipeline as pl

    async def fake(client, ref, flt, include_description=True, report=None):
        if ref.slug == "boom":
            raise AtsError("kaput")
        for i in range(50):
            yield _job(jobId=f"{ref.slug}-{i}", atsSource=ref.ats, companySlug=ref.slug)

    monkeypatch.setattr(pl, "fetch_company", fake)
    errors = []
    refs = [CompanyRef("lever", "a"), CompanyRef("lever", "boom"), CompanyRef("ashby", "b")]
    out = [j async for j in iter_jobs(None, refs, JobFilter(), max_items=10,
                                      on_error=lambda r, e: errors.append(r.slug))]
    assert len(out) == 10
    assert {j.companySlug for j in out} == {"a", "b"}
    assert abs(sum(j.companySlug == "a" for j in out) - 5) <= 1  # evenly spread
    assert errors == ["boom"]
