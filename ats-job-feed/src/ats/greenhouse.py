"""Greenhouse: boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"""

from __future__ import annotations

import re
from typing import AsyncIterator

from .filters import JobFilter
from .http import RateLimitedClient
from .models import CompanyRef, Job, RunReport
from .text import html_to_text

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\banywhere\b", re.I)


def _salary(job: dict) -> dict | None:
    ranges = job.get("pay_input_ranges") or []
    if not ranges:
        return None
    r = ranges[0]
    lo, hi = r.get("min_cents"), r.get("max_cents")
    return {
        "min": lo / 100 if lo is not None else None,
        "max": hi / 100 if hi is not None else None,
        "currency": r.get("currency_type"),
        "interval": "year",
        "summary": r.get("blurb") or r.get("title"),
    }


def normalize(raw: dict, ref: CompanyRef, include_description: bool) -> Job:
    location = (raw.get("location") or {}).get("name")
    offices = [o.get("name") for o in raw.get("offices") or [] if o.get("name")]
    locations = [location] if location else []
    locations += [o for o in offices if o not in locations]
    departments = [d.get("name") for d in raw.get("departments") or [] if d.get("name")]
    remote = any(_REMOTE_RE.search(x) for x in locations)
    return Job(
        title=(raw.get("title") or "").strip(),
        company=raw.get("company_name") or ref.slug.replace("-", " ").title(),
        location=location or (offices[0] if offices else None),
        remote=remote,
        department=departments[0] if departments else None,
        departments=departments,
        postedDate=raw.get("first_published") or raw.get("updated_at"),
        applyUrl=raw.get("absolute_url"),
        atsSource="greenhouse",
        descriptionText=html_to_text(raw.get("content")) if include_description else None,
        jobId=str(raw["id"]),
        salary=_salary(raw),
        jobUrl=raw.get("absolute_url"),
        locations=locations,
        companySlug=ref.slug,
    )


async def fetch_jobs(
    client: RateLimitedClient, ref: CompanyRef, flt: JobFilter, include_description: bool = True,
    report: RunReport | None = None,
) -> AsyncIterator[Job]:
    data = await client.get_json(API.format(slug=ref.slug), params={"content": "true"})
    for raw in data.get("jobs", []):
        job = normalize(raw, ref, include_description)
        if flt.match(job):
            yield job
