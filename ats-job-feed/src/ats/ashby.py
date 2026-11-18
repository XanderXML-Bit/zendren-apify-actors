"""Ashby: api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"""

from __future__ import annotations

from typing import AsyncIterator

from .filters import JobFilter
from .http import RateLimitedClient
from .models import CompanyRef, Job, RunReport
from .text import html_to_text

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def _salary(raw: dict) -> dict | None:
    comp = raw.get("compensation") or {}
    if not comp:
        return None
    salary = next(
        (c for c in comp.get("summaryComponents") or [] if c.get("compensationType") == "Salary"),
        None,
    )
    summary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
    if not salary and not summary:
        return None
    salary = salary or {}
    return {
        "min": salary.get("minValue"),
        "max": salary.get("maxValue"),
        "currency": salary.get("currencyCode"),
        "interval": salary.get("interval"),
        "summary": summary,
    }


def normalize(raw: dict, ref: CompanyRef, include_description: bool) -> Job:
    workplace = (raw.get("workplaceType") or "").lower()
    # Ashby sets isRemote=True for hybrid roles too, so workplaceType wins when present.
    remote = workplace == "remote" if workplace else bool(raw.get("isRemote"))
    primary = raw.get("location")
    locations = [primary] if primary else []
    for sec in raw.get("secondaryLocations") or []:
        name = sec.get("location") if isinstance(sec, dict) else sec
        if name and name not in locations:
            locations.append(name)
    return Job(
        title=(raw.get("title") or "").strip(),
        company=ref.slug.replace("-", " ").title(),
        location=primary,
        remote=remote,
        department=raw.get("department") or raw.get("team"),
        postedDate=raw.get("publishedAt"),
        applyUrl=raw.get("applyUrl") or raw.get("jobUrl"),
        atsSource="ashby",
        descriptionText=(
            (raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml")))
            if include_description else None
        ),
        jobId=str(raw["id"]),
        salary=_salary(raw),
        jobUrl=raw.get("jobUrl"),
        employmentType=raw.get("employmentType"),
        locations=locations,
        companySlug=ref.slug,
    )


async def fetch_jobs(
    client: RateLimitedClient, ref: CompanyRef, flt: JobFilter, include_description: bool = True,
    report: RunReport | None = None,
) -> AsyncIterator[Job]:
    data = await client.get_json(API.format(slug=ref.slug), params={"includeCompensation": "true"})
    for raw in data.get("jobs", []):
        if raw.get("isListed") is False:
            continue
        job = normalize(raw, ref, include_description)
        if flt.match(job):
            yield job
