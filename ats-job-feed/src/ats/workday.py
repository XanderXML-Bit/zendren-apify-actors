"""Workday CXS: POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

The list endpoint pages with ``limit`` (max 20) / ``offset``; the per-job detail
endpoint (same path + ``externalPath``) carries the description and the exact
start date.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import AsyncIterator

from .filters import JobFilter
from .http import RateLimitedClient
from .models import CompanyRef, Job, RunReport
from .text import html_to_text

log = logging.getLogger("jobfeed.workday")

PAGE_SIZE = 20
# Safety bound (3000 postings per search) so a bad tenant cannot loop forever. Hitting
# it with postings left is reported as truncation (log + SUMMARY), never silent.
MAX_PAGES = 150
MAX_DETAIL_FAILURE_LOGS = 5  # per company; the rest is only counted
_MULTI_LOC_RE = re.compile(r"^\d+\s+locations?$", re.I)
_POSTED_RE = re.compile(
    r"(\d+)\+?\s*(hour|day|week|month)s?\s+ago", re.I
)


def parse_posted_on(text: str | None, today: date | None = None) -> str | None:
    """'Posted Today' / 'Posted 3 Days Ago' -> ISO date; None if not derivable.

    'Posted 30+ Days Ago' is open-ended, so it yields None (the detail
    endpoint's startDate is used instead when descriptions are fetched).
    """
    if not text:
        return None
    today = today or datetime.now(timezone.utc).date()
    t = text.lower()
    if "+" in t:
        return None
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = _POSTED_RE.search(t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "hour":
        return today.isoformat()
    days = {"day": 1, "week": 7, "month": 30}[unit] * n
    return (today - timedelta(days=days)).isoformat()


def _base(ref: CompanyRef) -> str:
    return f"https://{ref.slug}.{ref.wd}.myworkdayjobs.com"


def _job_url(ref: CompanyRef, external_path: str) -> str:
    return f"{_base(ref)}/{ref.site}{external_path}"


_REQ_ID_RE = re.compile(r"[A-Za-z]*-?\d[\w-]*")


def job_id_from_list(posting: dict) -> str:
    """The posting's id, derived from the list response only.

    The id must not depend on whether the detail request ran or succeeded: it is the job's
    identity across runs (incremental mode) and restarts. So the detail's ``jobReqId`` is
    deliberately not used. Order: a requisition-like list bullet, else the requisition id
    parsed from the external path (``.../Title_R123`` gives ``R123``), else the path's last
    segment."""
    for bullet in reversed(posting.get("bulletFields") or []):
        if isinstance(bullet, str) and _REQ_ID_RE.fullmatch(bullet):
            return bullet
    last = (posting.get("externalPath") or "").rstrip("/").rsplit("/", 1)[-1]
    _, sep, tail = last.rpartition("_")
    if sep and _REQ_ID_RE.fullmatch(tail):
        return tail
    return last


def normalize(
    posting: dict, detail: dict | None, ref: CompanyRef, include_description: bool
) -> Job:
    info = (detail or {}).get("jobPostingInfo") or {}
    external_path = posting.get("externalPath") or ""
    job_id = job_id_from_list(posting)
    location = info.get("location") or posting.get("locationsText")
    locations = [location] if location else []
    locations += [x for x in info.get("additionalLocations") or [] if x not in locations]
    remote_type = (info.get("remoteType") or posting.get("remoteType") or "").lower()
    if remote_type:  # explicit workplace metadata is authoritative (onsite/hybrid => not remote)
        remote = "remote" in remote_type
    else:  # otherwise any of the locations (primary or additional) may say "Remote"
        remote = any(re.search(r"\bremote\b", loc, re.I) for loc in locations)
    posted = info.get("startDate") or parse_posted_on(
        info.get("postedOn") or posting.get("postedOn")
    )
    url = info.get("externalUrl") or _job_url(ref, external_path)
    return Job(
        title=(info.get("title") or posting.get("title") or "").strip(),
        company=ref.slug.replace("-", " ").title(),
        location=location,
        remote=remote,
        department=None,
        postedDate=posted,
        applyUrl=url,
        atsSource="workday",
        descriptionText=html_to_text(info.get("jobDescription")) if include_description else None,
        jobId=str(job_id),
        salary=None,
        jobUrl=url,
        employmentType=info.get("timeType") or posting.get("timeType"),
        locations=locations,
        companySlug=ref.slug,
    )


async def _fetch_detail(client: RateLimitedClient, ref: CompanyRef, posting: dict) -> dict | None:
    url = f"{_base(ref)}/wday/cxs/{ref.slug}/{ref.site}{posting['externalPath']}"
    return await client.get_json(url)


async def _list_postings(
    client: RateLimitedClient, ref: CompanyRef, search_text: str, report: RunReport
) -> AsyncIterator[list[dict]]:
    url = f"{_base(ref)}/wday/cxs/{ref.slug}/{ref.site}/jobs"
    offset, total = 0, None
    for _ in range(MAX_PAGES):
        data = await client.post_json(
            url,
            {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": search_text},
        )
        postings = data.get("jobPostings") or []
        if total is None:  # only the first page reports the real total
            total = data.get("total") or 0
        if not postings:
            return
        yield postings
        offset += PAGE_SIZE
        if len(postings) < PAGE_SIZE or offset >= total:
            return
    # page bound exhausted while the first-page total says there is more
    msg = (f"stopped after {MAX_PAGES * PAGE_SIZE} postings of {total} "
           f"(search {search_text!r}); later postings were not visited")
    log.warning("%s: results truncated: %s", ref.key, msg)
    report.truncated[ref.key] = (
        f"{report.truncated[ref.key]}; {msg}" if ref.key in report.truncated else msg
    )


def _needs_detail(p: dict, flt: JobFilter, include_description: bool) -> bool:
    """The list view hides locations behind "N Locations" and often omits
    remoteType, so fetch the detail when a filter needs those facts."""
    if include_description:
        return True
    if flt.remote_only and _remote_unknown(p):
        return True
    return bool(flt.location_alternatives) and _location_unknown(p)


def _location_unknown(p: dict) -> bool:
    text = p.get("locationsText") or ""
    return not text or bool(_MULTI_LOC_RE.match(text))


def _remote_unknown(p: dict) -> bool:
    return not p.get("remoteType") and "remote" not in (p.get("locationsText") or "").lower()


async def fetch_jobs(
    client: RateLimitedClient, ref: CompanyRef, flt: JobFilter, include_description: bool = True,
    report: RunReport | None = None,
) -> AsyncIterator[Job]:
    report = report if report is not None else RunReport()
    # Workday's own search is broader than our title/department match, so it is
    # only used to narrow the crawl; the strict filter is applied locally.
    searches = [" ".join(w) for w in flt.keyword_alternatives] or [""]
    seen: set[str] = set()
    for search_text in searches:
        async for page in _list_postings(client, ref, search_text, report):
            todo = []
            for p in page:
                path = p.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                if not flt.match_text(p.get("title", ""), None):
                    continue
                todo.append(p)

            details = await asyncio.gather(
                *(_fetch_detail(client, ref, p) if _needs_detail(p, flt, include_description)
                  else _none() for p in todo),
                return_exceptions=True,
            )
            for p, d in zip(todo, details):
                if isinstance(d, asyncio.CancelledError):
                    raise d
                location_unknown = remote_unknown = False
                if isinstance(d, Exception):
                    report.detail_failures[ref.key] += 1
                    n = report.detail_failures[ref.key]
                    (log.warning if n <= MAX_DETAIL_FAILURE_LOGS else log.debug)(
                        "%s: detail request failed for %s: %s: %s%s", ref.key, p.get("externalPath"),
                        type(d).__name__, d,
                        "" if n < MAX_DETAIL_FAILURE_LOGS else " (further failures only counted)",
                    )
                    d = None
                    # keep the list record; a fact only the detail had is unknown,
                    # which is not the same as a verified mismatch
                    location_unknown = bool(flt.location_alternatives) and _location_unknown(p)
                    remote_unknown = flt.remote_only and _remote_unknown(p)
                job = normalize(p, d, ref, include_description)
                if flt.match(job, location_unknown=location_unknown, remote_unknown=remote_unknown):
                    if location_unknown or remote_unknown:
                        report.unverified[ref.key] += 1
                    yield job


async def _none() -> None:
    return None
