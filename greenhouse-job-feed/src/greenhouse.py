"""Greenhouse Job Board API: board loading, hierarchy, filters, normalization.

Endpoints (public, no auth), all under ``https://boards-api.greenhouse.io/v1/boards/{token}``:
  ``""``                      board name
  ``/jobs?content=true``      jobs with departments[], offices[], metadata[], escaped HTML content
  ``/departments``            flat list with ``parent_id`` / ``child_ids`` (the hierarchy)
  ``/offices``                same for offices
  ``/jobs/{id}?questions=true&pay_transparency=true``   application questions + pay ranges

``content=true`` is always requested: without it the job objects carry no ``departments``/``offices``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Awaitable, Callable, Iterable

from .http import RateLimitedClient
from .models import BoardRef, GreenhouseError, HttpStatusError, Job, RunReport, job_key
from .text import html_to_markdown, html_to_text, unescape_content

API = "https://boards-api.greenhouse.io/v1/boards/{token}"
HOSTED_JOB_URL = "https://job-boards.greenhouse.io/{token}/jobs/{id}"
CONTENT_MODES = ("none", "text", "html", "markdown")
DATE_FIELDS = ("updatedAt", "firstPublishedAt")
DETAIL_WINDOW = 4   # concurrent per-job detail requests per board (the HTTP client also caps per host)

_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b|\banywhere\b", re.I)
# "remote" in a title is only a signal when it is not part of a product/domain phrase
_REMOTE_TITLE_RE = re.compile(
    r"\bremote\b(?!\s+(?:sensing|control|access|desktop|monitoring|operations center))", re.I)


# ---------------------------------------------------------------- hierarchy
def _norm(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


class Tree:
    """Departments or offices of one board: ``id -> {name, parent_id}`` plus path/ancestor lookups."""

    def __init__(self, nodes: Iterable[dict] = ()) -> None:
        self.names: dict[int, str] = {}
        self.parent: dict[int, int | None] = {}
        for n in nodes:
            self.add(n)

    def add(self, node: dict) -> None:
        nid = node.get("id")
        if nid is None or not node.get("name"):
            return
        self.names.setdefault(nid, node["name"])
        parent = node.get("parent_id")
        if nid not in self.parent or (self.parent[nid] is None and parent is not None):
            self.parent[nid] = parent

    def chain(self, nid: int) -> list[int]:
        """``[nid, parent, grandparent, ...]`` (known nodes only, cycle-safe)."""
        out: list[int] = []
        while nid is not None and nid in self.names and nid not in out:
            out.append(nid)
            nid = self.parent.get(nid)
        return out

    def path(self, nid: int) -> list[str]:
        return [self.names[i] for i in reversed(self.chain(nid))]

    def ids_named(self, names: Iterable[str]) -> tuple[set[int], list[str]]:
        """Ids whose name equals one of ``names`` (case/space-insensitive) and the names that matched nothing."""
        by_norm: dict[str, set[int]] = {}
        for nid, name in self.names.items():
            by_norm.setdefault(_norm(name), set()).add(nid)
        ids: set[int] = set()
        missing: list[str] = []
        for wanted in names:
            hit = by_norm.get(_norm(wanted))
            if hit:
                ids |= hit
            else:
                missing.append(wanted)
        return ids, missing

    def covers(self, selected: set[int], node_ids: Iterable[int]) -> bool:
        """True if any node, or any ancestor of one, is selected (a parent selects its children)."""
        return any(selected.intersection(self.chain(nid)) for nid in node_ids)

    def nested(self, members: dict[int, set] | None = None) -> list[dict]:
        """Nested ``[{id, name, jobs, children}]``; ``jobs`` = distinct open jobs in the node and all its
        descendants. ``members`` maps a node id to the ids of the jobs attached to that node directly,
        so a job attached to a parent and to its child (or to several children) counts once."""
        children: dict[int | None, list[int]] = {}
        for nid in self.names:
            parent = self.parent.get(nid)
            children.setdefault(parent if parent in self.names else None, []).append(nid)
        members = members or {}

        def build(nid: int, seen: frozenset) -> tuple[dict, set]:
            built = [build(c, seen | {nid}) for c in sorted(children.get(nid, []), key=lambda i: _norm(self.names[i]))
                     if c not in seen]
            ids = set(members.get(nid, ()))
            for _, kid_ids in built:
                ids |= kid_ids
            return {"id": nid, "name": self.names[nid], "jobs": len(ids), "children": [k for k, _ in built]}, ids

        return [build(r, frozenset())[0] for r in sorted(children.get(None, []), key=lambda i: _norm(self.names[i]))]


# ---------------------------------------------------------------- filters
def parse_updated_since(value, now: datetime | None = None) -> datetime | None:
    """``"2026-09-01"``, an ISO datetime, or relative (``"7 days"``, ``"2 weeks ago"``, ``"12 hours"``, ``"1 month"``)."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().lower()
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)\s*(h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|m|mo|month|months)(?:\s+ago)?", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("h"):
            delta = timedelta(hours=n)
        elif unit.startswith("d"):
            delta = timedelta(days=n)
        elif unit.startswith("w"):
            delta = timedelta(weeks=n)
        else:
            delta = timedelta(days=30 * n)
        return now - delta
    try:
        dt = datetime.fromisoformat(text.replace("z", "+00:00") if text.endswith("z") else text)
    except ValueError:
        raise ValueError(
            f"cannot read updatedSince={value!r}: use a date like 2026-09-01 or a relative value like '7 days'") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_api_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _terms(value: str | None) -> list[str]:
    return [t.strip().casefold() for t in (value or "").split(",") if t.strip()]


@dataclass
class JobFilter:
    keyword: str | None = None
    location: str | None = None
    remote_only: bool = False
    departments: list[str] = field(default_factory=list)
    offices: list[str] = field(default_factory=list)
    updated_since: datetime | None = None
    date_field: str = "updatedAt"

    def keyword_match(self, title: str, departments: list[str]) -> bool:
        alts = _terms(self.keyword)
        if not alts:
            return True
        hay = " ".join([title, *departments]).casefold()
        return any(all(w in hay for w in alt.split()) for alt in alts)

    def location_match(self, location: str | None, offices: list[str]) -> bool:
        alts = _terms(self.location)
        if not alts:
            return True
        hay = " | ".join([location or "", *offices]).casefold()
        return any(alt in hay for alt in alts)

    def date_match(self, raw: dict) -> bool:
        if self.updated_since is None:
            return True
        if self.date_field == "firstPublishedAt":
            dt = parse_api_date(raw.get("first_published")) or parse_api_date(raw.get("updated_at"))
        else:
            dt = parse_api_date(raw.get("updated_at"))
        return dt is not None and dt >= self.updated_since


def is_remote(location: str | None, offices: list[str], title: str, custom_fields: dict | None = None) -> bool:
    """Heuristic: 'remote' / 'work from home' / 'anywhere' in the location or an office name, a
    stand-alone 'remote' in the title, or a custom field named like *workplace/work type/location type*
    whose value says remote. Hybrid or on-site wording alone never sets it."""
    if any(_REMOTE_RE.search(x) for x in [location or "", *offices]):
        return True
    if _REMOTE_TITLE_RE.search(title or ""):
        return True
    for name, value in (custom_fields or {}).items():
        if re.search(r"workplace|work\s*type|work\s*model|location\s*type|remote", name, re.I):
            values = value if isinstance(value, list) else [value]
            if any(isinstance(v, str) and re.search(r"\bremote\b", v, re.I) and not re.search(r"hybrid|on-?site", v, re.I)
                   for v in values):
                return True
    return False


# ---------------------------------------------------------------- normalization
def custom_fields(raw: dict) -> dict:
    """``metadata[]`` -> ``{name: value}`` (values keep their type: string, list, bool, null)."""
    out: dict = {}
    for m in raw.get("metadata") or []:
        name = (m.get("name") or "").strip()
        if name:
            out[name] = m.get("value")
    return out


def _units(cents) -> int | float | None:
    if cents is None:
        return None
    value = cents / 100
    return int(value) if value == int(value) else round(value, 2)


def normalize_pay_ranges(raw: dict) -> list[dict]:
    """``pay_input_ranges[]`` (cents) -> ``[{min, max, currency, title, description}]`` in currency units."""
    out = []
    for r in raw.get("pay_input_ranges") or []:
        out.append({
            "min": _units(r.get("min_cents")),
            "max": _units(r.get("max_cents")),
            "currency": r.get("currency_type"),
            "title": (r.get("title") or "").strip() or None,
            "description": html_to_text(r.get("blurb")) or None,
        })
    return out


def normalize_questions(raw: dict, field_name: str = "questions", *, drop_hidden: bool = False) -> list[dict]:
    """Application form questions: one entry per input field.

    ``type`` is Greenhouse's field type (``input_text``, ``textarea``, ``input_file``,
    ``multi_value_single_select``, ``multi_value_multi_select``, ...); ``options`` is
    ``[{label, value}]`` for the select types and empty otherwise. ``drop_hidden`` leaves out
    ``input_hidden`` fields (the latitude/longitude helpers of the location group)."""
    return _flatten_questions(raw.get(field_name), drop_hidden)


def _flatten_questions(questions, drop_hidden: bool = False) -> list[dict]:
    out = []
    for q in questions or []:
        fields = q.get("fields") or [{}]
        if drop_hidden:
            fields = [f for f in fields if f.get("type") != "input_hidden"]
        for f in fields:
            out.append({
                "label": (q.get("label") or "").strip(),
                "required": bool(q.get("required")),
                "type": f.get("type"),
                "name": f.get("name"),
                "description": html_to_text(q.get("description")) or None,
                "options": [{"label": v.get("label"), "value": v.get("value")} for v in f.get("values") or []],
            })
    return out


def normalize_location_questions(raw: dict) -> list[dict]:
    """The location group (``location_questions``), without its hidden latitude/longitude fields."""
    return normalize_questions(raw, "location_questions", drop_hidden=True)


def normalize_compliance_questions(raw: dict) -> list[dict]:
    """Compliance groups (``compliance``, e.g. EEOC): ``[{type, description, questions}]``, the questions
    shaped like :func:`normalize_questions`."""
    return [{
        "type": g.get("type"),
        "description": html_to_text(g.get("description")) or None,
        "questions": _flatten_questions(g.get("questions")),
    } for g in raw.get("compliance") or []]


def normalize_demographic_questions(raw: dict) -> dict | None:
    """The demographic survey (``demographic_questions``): ``{header, description, questions}`` where each
    question is ``{id, label, required, type, options: [{label, value, freeForm}]}``; ``None`` when absent."""
    demo = raw.get("demographic_questions")
    if not isinstance(demo, dict) or not demo.get("questions"):
        return None
    return {
        "header": (demo.get("header") or "").strip() or None,
        "description": html_to_text(demo.get("description")) or None,
        "questions": [{
            "id": q.get("id"),
            "label": (q.get("label") or "").strip(),
            "required": bool(q.get("required")),
            "type": q.get("type"),
            "options": [{"label": a.get("label"), "value": a.get("id"), "freeForm": bool(a.get("free_form"))}
                        for a in q.get("answer_options") or []],
        } for q in demo["questions"]],
    }


def _render_content(raw_html: str | None, mode: str) -> str | None:
    if mode == "none":
        return None
    if mode == "html":
        return unescape_content(raw_html)
    if mode == "markdown":
        return html_to_markdown(raw_html)
    return html_to_text(raw_html)


@dataclass
class Options:
    include_content: str = "text"
    include_pay_ranges: bool = False
    include_questions: bool = False
    include_custom_fields: bool = True
    only_new: bool = False


def build_item(raw: dict, token: str, board_name: str | None, opts: Options,
               dept_tree: Tree, office_tree: Tree, scraped_at: str) -> dict:
    location = (raw.get("location") or {}).get("name")
    depts = [d for d in raw.get("departments") or [] if d.get("name")]
    offs = [o for o in raw.get("offices") or [] if o.get("name")]
    fields = custom_fields(raw)
    title = (raw.get("title") or "").strip()
    office_names = [o["name"] for o in offs]
    item = {
        "jobId": raw.get("id"),
        "internalJobId": raw.get("internal_job_id"),
        "requisitionId": raw.get("requisition_id") or None,
        "title": title,
        "boardToken": token,
        "companyName": raw.get("company_name") or board_name or token,
        "location": location or (office_names[0] if office_names else None),
        "offices": office_names,
        "officePath": office_tree.path(offs[0]["id"]) if offs else [],
        "departments": [d["name"] for d in depts],
        "departmentPath": dept_tree.path(depts[0]["id"]) if depts else [],
        "remote": is_remote(location, office_names, title, fields),
        "absoluteUrl": raw.get("absolute_url"),
        "applyUrl": HOSTED_JOB_URL.format(token=token, id=raw.get("id")),
        "updatedAt": raw.get("updated_at"),
        "firstPublishedAt": raw.get("first_published"),
        "language": raw.get("language"),
    }
    if opts.include_content != "none":
        item["content"] = _render_content(raw.get("content"), opts.include_content)
    if opts.include_custom_fields:
        item["customFields"] = fields
    item["scrapedAt"] = scraped_at
    return item


# ---------------------------------------------------------------- fetching
@dataclass
class Board:
    ref: BoardRef
    name: str | None
    raw_jobs: list[dict]
    departments: Tree
    offices: Tree
    notes: dict = field(default_factory=dict)


async def load_board(client: RateLimitedClient, ref: BoardRef, report: RunReport | None = None) -> Board:
    """Board info, jobs (content=true) and the two hierarchies. A failing hierarchy request only
    degrades the paths/filters (reported); a failing board/jobs request fails the board."""
    base = API.format(token=ref.token)
    try:
        info = await client.get_json(base)
    except HttpStatusError as exc:
        if exc.status == 404:
            raise GreenhouseError(f"board {ref.token!r} not found (check the token)") from exc
        raise
    data = await client.get_json(f"{base}/jobs", params={"content": "true"})
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise GreenhouseError(f"unexpected jobs response for board {ref.token!r}")

    async def tree(kind: str, key: str) -> Tree:
        try:
            payload = await client.get_json(f"{base}/{kind}")
            return Tree(payload.get(key) or [])
        except GreenhouseError as exc:
            if report is not None:
                prev = report.hierarchy_unavailable.get(ref.token)
                report.hierarchy_unavailable[ref.token] = f"{prev}; {kind}: {exc}" if prev else f"{kind}: {exc}"
            return Tree()

    departments, offices = await asyncio.gather(tree("departments", "departments"), tree("offices", "offices"))
    # nodes only mentioned by jobs (e.g. a hierarchy request failed) still get a name and parent
    for raw in jobs:
        for d in raw.get("departments") or []:
            departments.add(d)
        for o in raw.get("offices") or []:
            offices.add(o)
    return Board(ref, (info or {}).get("name"), jobs, departments, offices)


async def fetch_detail(client: RateLimitedClient, token: str, job_id, opts: Options) -> dict:
    params = {}
    if opts.include_questions:
        params["questions"] = "true"
    if opts.include_pay_ranges:
        params["pay_transparency"] = "true"
    return await client.get_json(f"{API.format(token=token)}/jobs/{job_id}", params=params)


async def ordered_concurrent(items: Iterable, fn: Callable[..., Awaitable], window: int) -> AsyncIterator:
    """Run ``fn(item)`` for up to ``window`` items ahead; yield results in input order."""
    it = iter(items)
    pending: deque[asyncio.Task] = deque()
    try:
        while True:
            while len(pending) < window:
                try:
                    item = next(it)
                except StopIteration:
                    break
                pending.append(asyncio.ensure_future(fn(item)))
            if not pending:
                return
            yield await pending.popleft()
    finally:
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def _direct_members(raw_jobs: list[dict], field_name: str) -> dict[int, set]:
    """``{node id: {ids of the jobs attached to that node}}`` (a job without an id still counts once)."""
    members: dict[int, set] = {}
    for index, raw in enumerate(raw_jobs):
        job = raw.get("id") if raw.get("id") is not None else ("anonymous", index)
        for node in raw.get(field_name) or []:
            if node.get("id") is not None:
                members.setdefault(node["id"], set()).add(job)
    return members


def _sort_key(raw: dict) -> float:
    dt = parse_api_date(raw.get("updated_at")) or parse_api_date(raw.get("first_published"))
    return -(dt.timestamp() if dt else 0.0)


async def fetch_jobs(
    client: RateLimitedClient,
    ref: BoardRef,
    flt: JobFilter,
    opts: Options,
    *,
    seen: set[str] | None = None,
    known: set[str] | None = None,
    touch: Callable[[str], None] | None = None,
    report: RunReport | None = None,
    board_gate: asyncio.Semaphore | None = None,
) -> AsyncIterator[Job]:
    """Yield the board's matching jobs, most recently updated first.

    ``seen``: keys to skip (delivered in this run before a restart, or known from the incremental state);
    ``known``: the incremental subset of those (counted as ``alreadySeen``); ``touch(key)`` is called
    for every listed job that is in the incremental state, so its last-seen date is refreshed."""
    seen = seen if seen is not None else set()
    report = report or RunReport()
    token = ref.token
    if board_gate is not None:
        async with board_gate:  # bounds how many large job lists sit in memory at once
            board = await load_board(client, ref, report)
    else:
        board = await load_board(client, ref, report)
    report.names[token] = board.name or token
    report.on_board[token] = len(board.raw_jobs)
    report.trees[token] = {
        "name": board.name,
        "departments": board.departments.nested(_direct_members(board.raw_jobs, "departments")),
        "offices": board.offices.nested(_direct_members(board.raw_jobs, "offices")),
    }

    dept_ids, missing_depts = board.departments.ids_named(flt.departments)
    office_ids, missing_offices = board.offices.ids_named(flt.offices)
    if missing_depts or missing_offices:
        report.unmatched_filters[token] = {"departments": missing_depts, "offices": missing_offices}

    scraped_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    candidates: list[tuple[str, dict]] = []   # (key, raw)
    matched = 0
    for raw in sorted(board.raw_jobs, key=_sort_key):
        if raw.get("id") is None:
            continue
        key = job_key(token, raw["id"])
        if touch and known and key in known:
            touch(key)
        depts = raw.get("departments") or []
        offs = raw.get("offices") or []
        dept_names = [d.get("name") for d in depts if d.get("name")]
        office_names = [o.get("name") for o in offs if o.get("name")]
        location = (raw.get("location") or {}).get("name")
        if flt.departments and not board.departments.covers(dept_ids, [d["id"] for d in depts if "id" in d]):
            continue
        if flt.offices and not board.offices.covers(office_ids, [o["id"] for o in offs if "id" in o]):
            continue
        if not flt.keyword_match(raw.get("title") or "", dept_names):
            continue
        if not flt.location_match(location, office_names):
            continue
        if not flt.date_match(raw):
            continue
        fields = custom_fields(raw)
        if flt.remote_only and not is_remote(location, office_names, raw.get("title") or "", fields):
            continue
        matched += 1
        if key in seen:
            if known and key in known:
                report.already_seen[token] = report.already_seen.get(token, 0) + 1
            continue
        candidates.append((key, raw))
    report.matched[token] = matched
    report.changed()

    needs_detail = opts.include_pay_ranges or opts.include_questions

    async def build(pair: tuple[str, dict]) -> Job | None:
        key, raw = pair
        item = build_item(raw, token, board.name, opts, board.departments, board.offices, scraped_at)
        if needs_detail:
            try:
                detail = await fetch_detail(client, token, raw["id"], opts)
            except GreenhouseError as exc:
                gone = isinstance(exc, HttpStatusError) and exc.status == 404
                if not gone:
                    report.detail_failures.setdefault(token, []).append(raw["id"])
                    report.changed()
                if gone or opts.only_new:
                    return None  # removed meanwhile, or (incremental) retried on the next run instead of lost
                detail = None
            if opts.include_pay_ranges:
                item["payRanges"] = normalize_pay_ranges(detail) if detail is not None else None
            if opts.include_questions:
                have = detail is not None
                item["questions"] = normalize_questions(detail) if have else None
                item["locationQuestions"] = normalize_location_questions(detail) if have else None
                item["complianceQuestions"] = normalize_compliance_questions(detail) if have else None
                item["demographicQuestions"] = normalize_demographic_questions(detail) if have else None
        item["scrapedAt"] = item.pop("scrapedAt")  # keep it last
        return Job(token, raw["id"], item)

    window = DETAIL_WINDOW if needs_detail else 1
    async with contextlib.aclosing(ordered_concurrent(candidates, build, window)) as results:
        async for job in results:
            if job is not None:
                yield job

