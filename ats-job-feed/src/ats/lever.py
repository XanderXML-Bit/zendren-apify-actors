"""Lever: api.lever.co/v0/postings/{slug}?mode=json (EU: api.eu.lever.co)"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

from .filters import JobFilter
from .http import RateLimitedClient
from .models import CompanyRef, HttpStatusError, Job, RunReport
from .text import html_to_text

HOSTS = {None: "https://api.lever.co", "eu": "https://api.eu.lever.co"}


def _description(raw: dict) -> str:
    parts = [raw.get("description") or raw.get("descriptionBody") or ""]
    for item in raw.get("lists") or []:
        parts.append(f"<h3>{item.get('text', '')}</h3>{item.get('content', '')}")
    parts.append(raw.get("additional") or "")
    text = "\n\n".join(p for p in (html_to_text(x) for x in parts) if p)
    return text or (raw.get("descriptionPlain") or "").strip()


def _salary(raw: dict) -> dict | None:
    sr = raw.get("salaryRange")
    if not sr:
        return None
    return {
        "min": sr.get("min"),
        "max": sr.get("max"),
        "currency": sr.get("currency"),
        "interval": sr.get("interval"),
        "summary": (raw.get("salaryDescriptionPlain") or "").strip() or None,
    }


def normalize(raw: dict, ref: CompanyRef, include_description: bool) -> Job:
    cats = raw.get("categories") or {}
    location = cats.get("location")
    all_locs = [x for x in (cats.get("allLocations") or []) if x]
    if location and location not in all_locs:
        all_locs.insert(0, location)
    created = raw.get("createdAt")
    posted = (
        datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
        if isinstance(created, (int, float)) else None
    )
    workplace = (raw.get("workplaceType") or "").lower()
    remote = workplace == "remote" or (not workplace and "remote" in (location or "").lower())
    return Job(
        title=(raw.get("text") or "").strip(),
        company=ref.slug.replace("-", " ").title(),
        location=location,
        remote=remote,
        department=cats.get("department") or cats.get("team"),
        postedDate=posted,
        applyUrl=raw.get("applyUrl") or raw.get("hostedUrl"),
        atsSource="lever",
        descriptionText=_description(raw) if include_description else None,
        jobId=str(raw["id"]),
        salary=_salary(raw),
        jobUrl=raw.get("hostedUrl"),
        employmentType=cats.get("commitment"),
        locations=all_locs,
        companySlug=ref.slug,
    )


async def fetch_jobs(
    client: RateLimitedClient, ref: CompanyRef, flt: JobFilter, include_description: bool = True,
    report: RunReport | None = None,
) -> AsyncIterator[Job]:
    regions = [ref.region] if ref.region else [None, "eu"]  # fall back to EU on 404
    data = None
    for i, region in enumerate(regions):
        try:
            data = await client.get_json(
                f"{HOSTS[region]}/v0/postings/{ref.slug}", params={"mode": "json"}
            )
            break
        except HttpStatusError as exc:
            if exc.status != 404 or i == len(regions) - 1:
                raise
    for raw in data or []:
        job = normalize(raw, ref, include_description)
        if flt.match(job):
            yield job
