"""Lever Postings API: query building, paging, region fallback, filters, normalization, discovery.

Verified live (api.lever.co / api.eu.lever.co, ``GET /v0/postings/{site}?mode=json``):
- ``team``, ``department``, ``commitment`` and ``level`` match the whole value, case-insensitively;
  ``location`` matches the whole value case-sensitively and against *all* of a posting's locations.
- Repeating a parameter is OR; different parameters are AND. Unknown values return ``[]``.
- ``skip``/``limit`` page the list; an unknown site is HTTP 404 ``{"ok": false, ...}``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .http import HttpStatusError, LeverError, RateLimitedClient
from .sites import EU, GLOBAL, HOSTS, SiteRef
from .text import html_to_markdown, html_to_text, list_items

PAGE_SIZE = 100
MAX_PAGES = 200  # 20,000 postings: far beyond any real Lever site, only a loop guard

WORKPLACE_TYPES = ("remote", "hybrid", "on-site", "unspecified")
DESCRIPTION_FORMATS = ("none", "text", "html", "markdown")


# ---------------------------------------------------------------- server-side filters
@dataclass
class ServerFilters:
    """Filters Lever applies itself (so excluded postings are never downloaded)."""

    teams: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    commitments: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)

    @classmethod
    def from_input(cls, inp: dict) -> "ServerFilters":
        return cls(**{name: clean_values(inp.get(name)) for name in
                      ("teams", "departments", "locations", "commitments", "levels")})

    def params(self) -> dict:
        """Query parameters (list values are sent as repeated parameters = OR)."""
        out: dict = {}
        for api_name, values in (("team", self.teams), ("department", self.departments),
                                 ("location", self.locations), ("commitment", self.commitments),
                                 ("level", self.levels)):
            if values:
                out[api_name] = list(values)
        return out

    def applied(self) -> dict:
        return {k: list(v) for k, v in (("teams", self.teams), ("departments", self.departments),
                                       ("locations", self.locations), ("commitments", self.commitments),
                                       ("levels", self.levels)) if v}


def clean_values(raw) -> list[str]:
    """Trimmed, de-duplicated, non-empty strings (order kept). Values are NOT split on commas:
    Lever values such as ``Berlin, Germany`` contain commas."""
    out: list[str] = []
    for v in raw or []:
        if isinstance(v, str) and v.strip() and v.strip() not in out:
            out.append(v.strip())
    return out


# ---------------------------------------------------------------- fetching
async def _fetch_pages(client: RateLimitedClient, region: str, site: str, params: dict) -> list[dict]:
    url = f"{HOSTS[region]}/v0/postings/{site}"
    out: list[dict] = []
    seen_ids: set[str] = set()
    for page in range(MAX_PAGES):
        data = await client.get_json(url, params={"mode": "json", **params,
                                                  "skip": page * PAGE_SIZE, "limit": PAGE_SIZE})
        if not isinstance(data, list):
            raise LeverError(f"unexpected response from {url} (not a list)")
        new = [p for p in data if isinstance(p, dict) and p.get("id") and p["id"] not in seen_ids]
        for p in new:
            seen_ids.add(p["id"])
        out.extend(new)
        if len(data) < PAGE_SIZE or not new:  # last page, or a server that ignores skip
            break
    return out


def regions_to_try(ref: SiteRef, region_setting: str) -> list[str]:
    if region_setting in (GLOBAL, EU):
        return [region_setting]
    return [ref.region] if ref.region else [GLOBAL, EU]


async def fetch_site(client: RateLimitedClient, ref: SiteRef, region_setting: str,
                     server: ServerFilters) -> tuple[str, list[dict]]:
    """All postings of a site matching the server-side filters, plus the region that served them.

    With no region known the global instance is tried first and the EU instance on 404."""
    regions = regions_to_try(ref, region_setting)
    for i, region in enumerate(regions):
        try:
            return region, await _fetch_pages(client, region, ref.site, server.params())
        except HttpStatusError as exc:
            if exc.status != 404:
                raise
            if i == len(regions) - 1:
                where = " or ".join(f"{HOSTS[r].split('//')[1]}" for r in regions)
                raise LeverError(f"site {ref.site!r} not found on {where} (HTTP 404)") from exc
    raise LeverError("no region to try")  # unreachable


# ---------------------------------------------------------------- client-side filters
_REL = re.compile(r"^(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|m|mo|month|months|y|yr|year|years)"
                  r"(?:\s+ago)?$", re.I)
_UNIT_HOURS = {"h": 1, "d": 24, "w": 168, "m": 720, "y": 8760}


def parse_posted_since(value, now: datetime | None = None) -> datetime | None:
    """``"7 days"``, ``"2 weeks ago"``, ``"36h"``, ``"2026-10-01"`` or an ISO timestamp -> UTC datetime."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    now = now or datetime.now(timezone.utc)
    text = str(value).strip()
    m = _REL.match(text)
    if m:
        unit = m.group(2).lower()
        key = "m" if unit in ("m", "mo", "month", "months") else unit[0]
        return now - timedelta(hours=float(m.group(1)) * _UNIT_HOURS[key])
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"cannot read postedSince {text!r}: use a date like 2026-10-01 or "
                         f"a relative value like '7 days'") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_workplace(value) -> str:
    v = re.sub(r"[\s_]+", "-", str(value or "").strip().lower())
    if v in ("onsite", "on-site", "on-premise", "onpremise"):
        return "on-site"
    if v in ("remote", "hybrid"):
        return v
    return "unspecified"


def normalize_workplace_types(raw) -> set[str]:
    out = set()
    for v in raw or []:
        n = normalize_workplace(v)
        if n == "unspecified" and str(v).strip().lower() != "unspecified":
            raise ValueError(f"unknown workplace type {v!r}: use remote, hybrid, on-site or unspecified")
        out.add(n)
    return out


@dataclass
class ClientFilters:
    keyword: str | None = None
    workplace_types: set[str] = field(default_factory=set)
    posted_since: datetime | None = None
    salary_only: bool = False

    def __post_init__(self) -> None:
        # "a b, c" -> [[a, b], [c]]: words AND within a group, groups OR
        self._groups = [[w for w in g.lower().split() if w] for g in (self.keyword or "").split(",")]
        self._groups = [g for g in self._groups if g]

    def match(self, raw: dict) -> bool:
        cats = raw.get("categories") or {}
        if self._groups:
            hay = " ".join(str(x) for x in (raw.get("text"), cats.get("team"), cats.get("department")) if x).lower()
            if not any(all(w in hay for w in g) for g in self._groups):
                return False
        if self.workplace_types and normalize_workplace(raw.get("workplaceType")) not in self.workplace_types:
            return False
        if self.posted_since is not None:
            created = raw.get("createdAt")
            if not isinstance(created, (int, float)) or created < self.posted_since.timestamp() * 1000:
                return False
        if self.salary_only and normalize_salary(raw) is None:
            return False
        return True


# ---------------------------------------------------------------- normalization
_INTERVAL = re.compile(r"^per-(.+?)-(?:salary|wage)$")


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v) if float(v).is_integer() else v


def normalize_salary(raw: dict) -> dict | None:
    sr = raw.get("salaryRange")
    if not isinstance(sr, dict):
        return None
    lo, hi = _num(sr.get("min")), _num(sr.get("max"))
    if lo is None and hi is None:
        return None
    interval = sr.get("interval")
    if isinstance(interval, str) and interval:
        m = _INTERVAL.match(interval)
        interval = (m.group(1).replace("-", "") if m else interval).lower()
        interval = {"yearly": "year", "monthly": "month", "weekly": "week", "daily": "day", "hourly": "hour"}.get(interval, interval)
    else:
        interval = None
    currency = sr.get("currency")
    return {"min": lo, "max": hi, "currency": currency.upper() if isinstance(currency, str) and currency else None,
            "interval": interval}


def salary_text(salary: dict | None) -> str | None:
    """``USD 123,000–168,000 per year`` (``from``/``up to`` when only one bound is published)."""
    if not salary:
        return None
    lo, hi = salary["min"], salary["max"]
    fmt = lambda n: f"{n:,}"  # noqa: E731
    if lo is not None and hi is not None:
        amount = fmt(lo) if lo == hi else f"{fmt(lo)}–{fmt(hi)}"
    elif lo is not None:
        amount = f"from {fmt(lo)}"
    else:
        amount = f"up to {fmt(hi)}"
    parts = [salary["currency"], amount] if salary["currency"] else [amount]
    text = " ".join(parts)
    return f"{text} per {salary['interval']}" if salary["interval"] else text


def created_iso(raw: dict) -> str | None:
    created = raw.get("createdAt")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def render(html_value, plain_value, fmt: str) -> str | None:
    """One description section in the requested format (``None`` for ``none`` or an empty section)."""
    if fmt == "none":
        return None
    if fmt == "html":
        out = (html_value or "").strip()
    elif fmt == "markdown":
        out = html_to_markdown(html_value) if html_value else (plain_value or "").strip()
    else:
        out = (plain_value or "").strip() or html_to_text(html_value)
    return out or None


def parse_lists(raw: dict) -> list[dict]:
    """Requirement/responsibility sections as ``[{"heading": ..., "items": [...]}]`` (plain text items)."""
    out = []
    for entry in raw.get("lists") or []:
        if not isinstance(entry, dict):
            continue
        items = list_items(entry.get("content"))
        heading = re.sub(r"\s+", " ", str(entry.get("text") or "")).strip() or None
        if items or heading:
            out.append({"heading": heading, "items": items})
    return out


def all_locations(raw: dict) -> list[str]:
    cats = raw.get("categories") or {}
    locs = [x for x in (cats.get("allLocations") or []) if isinstance(x, str) and x]
    loc = cats.get("location")
    if isinstance(loc, str) and loc:
        locs = [loc] + locs  # the primary location first
    return list(dict.fromkeys(locs))  # each location once, first occurrence kept


def description_body(raw: dict) -> tuple[str | None, str | None]:
    """(html, plain) of the description *body*, without the opening.

    Lever's ``description``/``descriptionPlain`` are the opening and the body combined, so they are
    only a compatibility fallback when the body fields are absent altogether. A body that is present
    but empty means "no body" (an opening-only posting) and must not bring the opening back."""
    if "descriptionBody" in raw or "descriptionBodyPlain" in raw:
        return raw.get("descriptionBody"), raw.get("descriptionBodyPlain")
    return raw.get("description"), raw.get("descriptionPlain")


def normalize(raw: dict, site: str, region: str, description_format: str, include_lists: bool,
              scraped_at: str) -> dict:
    cats = raw.get("categories") or {}
    workplace = normalize_workplace(raw.get("workplaceType"))
    salary = normalize_salary(raw)
    body_html, body_plain = description_body(raw)
    return {
        "postingId": str(raw["id"]),
        "title": (raw.get("text") or "").strip(),
        "site": site,
        "region": region,
        "team": cats.get("team") or None,
        "department": cats.get("department") or None,
        "location": cats.get("location") or None,
        "allLocations": all_locations(raw),
        "country": raw.get("country") or None,
        "commitment": cats.get("commitment") or None,
        "level": cats.get("level") or None,
        "workplaceType": workplace,
        "remote": workplace == "remote",
        "salary": salary,
        "salaryText": salary_text(salary),
        "salaryDescription": render(raw.get("salaryDescription"), raw.get("salaryDescriptionPlain"),
                                    description_format),
        "hostedUrl": raw.get("hostedUrl"),
        "applyUrl": raw.get("applyUrl"),
        "createdAt": created_iso(raw),
        "opening": render(raw.get("opening"), raw.get("openingPlain"), description_format),
        "description": render(body_html, body_plain, description_format),
        "additional": render(raw.get("additional"), raw.get("additionalPlain"), description_format),
        "lists": parse_lists(raw) if include_lists else None,
        "scrapedAt": scraped_at,
    }


# ---------------------------------------------------------------- discovery
def _ranked(counter: Counter) -> list[dict]:
    return [{"value": v, "count": n} for v, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].lower()))]


def discover(raw_postings: list[dict], site: str, region: str, scraped_at: str) -> dict:
    """One record with the distinct filter values of a whole board and how many postings carry each.

    ``locations`` counts every location of a posting (what Lever's ``location`` filter matches)."""
    teams, depts, locs, commits, levels, wps = (Counter() for _ in range(6))
    for raw in raw_postings:
        cats = raw.get("categories") or {}
        for counter, value in ((teams, cats.get("team")), (depts, cats.get("department")),
                               (commits, cats.get("commitment")), (levels, cats.get("level"))):
            if isinstance(value, str) and value:
                counter[value] += 1
        for loc in all_locations(raw):
            locs[loc] += 1
        wps[normalize_workplace(raw.get("workplaceType"))] += 1
    return {
        "recordType": "filters",
        "site": site,
        "region": region,
        "totalPostings": len(raw_postings),
        "teams": _ranked(teams),
        "departments": _ranked(depts),
        "locations": _ranked(locs),
        "commitments": _ranked(commits),
        "levels": _ranked(levels),
        "workplaceTypes": _ranked(wps),
        "scrapedAt": scraped_at,
    }
