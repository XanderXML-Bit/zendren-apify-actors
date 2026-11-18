"""A small, realistic fake Greenhouse board and an offline client for it."""

from __future__ import annotations

import copy
from urllib.parse import urlsplit

from src.models import HttpStatusError

# department tree: Engineering > Backend, Frontend ; Sales
DEPARTMENTS = [
    {"id": 1, "name": "Engineering", "parent_id": None, "child_ids": [2, 3], "jobs": []},
    {"id": 2, "name": "Backend", "parent_id": 1, "child_ids": [], "jobs": []},
    {"id": 3, "name": "Frontend", "parent_id": 1, "child_ids": [], "jobs": []},
    {"id": 4, "name": "Sales", "parent_id": None, "child_ids": [], "jobs": []},
]
# office tree: North America > USA > San Francisco, New York ; EMEA > London
OFFICES = [
    {"id": 10, "name": "North America", "location": None, "parent_id": None, "child_ids": [11], "departments": []},
    {"id": 11, "name": "USA", "location": None, "parent_id": 10, "child_ids": [12, 13], "departments": []},
    {"id": 12, "name": "San Francisco", "location": "San Francisco, CA", "parent_id": 11, "child_ids": [], "departments": []},
    {"id": 13, "name": "New York", "location": "New York, NY", "parent_id": 11, "child_ids": [], "departments": []},
    {"id": 20, "name": "EMEA", "location": None, "parent_id": None, "child_ids": [21], "departments": []},
    {"id": 21, "name": "London", "location": "London, UK", "parent_id": 20, "child_ids": [], "departments": []},
]
ESCAPED = ("&lt;h2&gt;About&lt;/h2&gt;&lt;p&gt;Build &lt;strong&gt;great&lt;/strong&gt; things &amp;amp; ship.&lt;/p&gt;"
           "&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;/ul&gt;")


def dept(i):
    d = next(x for x in DEPARTMENTS if x["id"] == i)
    return {"id": i, "name": d["name"], "child_ids": d["child_ids"], "parent_id": d["parent_id"]}


def office(i):
    o = next(x for x in OFFICES if x["id"] == i)
    return {"id": i, "name": o["name"], "location": o["location"], "child_ids": o["child_ids"], "parent_id": o["parent_id"]}


def make_job(i, *, title=None, depts=(2,), offices=(12,), location=None, updated="2026-10-10T10:00:00-04:00",
             first_published="2026-09-01T10:00:00-04:00", metadata=None, company="Acme"):
    return {
        "id": 1000 + i, "internal_job_id": 5000 + i, "requisition_id": f"R{i}",
        "title": title or f"Job {i}", "updated_at": updated, "first_published": first_published,
        "absolute_url": f"https://careers.acme.test/jobs/{1000 + i}?gh_jid={1000 + i}",
        "location": {"name": location if location is not None else "San Francisco, CA"},
        "company_name": company, "language": "en", "content": ESCAPED,
        "metadata": metadata if metadata is not None else [
            {"id": 1, "name": "Employment Type", "value": "Full time", "value_type": "single_select"},
            {"id": 2, "name": "Skills", "value": ["Python", "SQL"], "value_type": "multi_select"},
            {"id": 3, "name": "Relocation", "value": True, "value_type": "yes_no"},
        ],
        "departments": [dept(d) for d in depts], "offices": [office(o) for o in offices],
    }


DETAIL = {
    "pay_input_ranges": [
        {"min_cents": 15000000, "max_cents": 20000050, "currency_type": "USD", "title": "US Zone 1",
         "blurb": "&lt;p&gt;Base pay &lt;b&gt;only&lt;/b&gt;&lt;/p&gt;"},
        {"min_cents": None, "max_cents": 4000, "currency_type": "EUR", "title": "Hourly", "blurb": ""},
    ],
    "questions": [
        {"label": "First Name", "required": True, "description": None,
         "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True, "description": "&lt;p&gt;PDF please&lt;/p&gt;",
         "fields": [{"name": "resume", "type": "input_file", "values": []},
                    {"name": "resume_text", "type": "textarea", "values": []}]},
        {"label": "Work authorization?", "required": False, "description": None,
         "fields": [{"name": "question_1", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
    ],
    "location_questions": [
        {"label": "Location", "required": True, "description": None,
         "fields": [{"name": "job_application_location", "type": "input_text", "values": []}]},
        {"label": "Latitude", "required": True, "fields": [{"name": "latitude", "type": "input_hidden", "values": []}]},
        {"label": "Longitude", "required": True, "fields": [{"name": "longitude", "type": "input_hidden", "values": []}]},
    ],
    "compliance": [
        {"type": "eeoc", "description": "&lt;p&gt;Voluntary self-identification&lt;/p&gt;", "questions": [
            {"label": "Gender", "required": False, "description": None,
             "fields": [{"name": "gender", "type": "multi_value_single_select",
                         "values": [{"label": "Female", "value": 1}, {"label": "Decline", "value": 3}]}]},
        ]},
    ],
    "demographic_questions": {
        "header": "Diversity survey", "description": "&lt;p&gt;Optional&lt;/p&gt;",
        "questions": [
            {"id": 77, "label": "Which describes you?", "required": False, "type": "multi_value_multi_select",
             "answer_options": [{"id": 701, "label": "A", "free_form": False}, {"id": 702, "label": "Other", "free_form": True}]},
        ],
    },
}


class FakeClient:
    """Serves ``boards[token] = {"name", "jobs", "departments", "offices", "details"}`` from memory.

    ``fail`` maps a path suffix (e.g. ``"/departments"``, ``"/jobs/1001"``) to an exception to raise.
    ``calls`` records every (path, params) request.
    """

    def __init__(self, boards: dict, fail: dict | None = None):
        self.boards = boards
        self.fail = fail or {}
        self.calls: list[tuple[str, dict]] = []

    async def get_json(self, url, params=None):
        path = urlsplit(url).path
        self.calls.append((path, dict(params or {})))
        for suffix, exc in self.fail.items():
            if path.endswith(suffix):
                raise exc
        parts = path.split("/")  # '', 'v1', 'boards', token, ...
        token = parts[3]
        board = self.boards.get(token)
        if board is None:
            raise HttpStatusError(404, url)
        rest = parts[4:]
        if not rest:
            return {"name": board["name"], "content": ""}
        if rest == ["jobs"]:
            assert (params or {}).get("content") == "true"
            return {"jobs": copy.deepcopy(board["jobs"]), "meta": {"total": len(board["jobs"])}}
        if rest == ["departments"]:
            return {"departments": copy.deepcopy(board.get("departments", DEPARTMENTS))}
        if rest == ["offices"]:
            return {"offices": copy.deepcopy(board.get("offices", OFFICES))}
        if rest[0] == "jobs" and len(rest) == 2:
            job_id = int(rest[1])
            job = next((j for j in board["jobs"] if j["id"] == job_id), None)
            if job is None:
                raise HttpStatusError(404, url)
            out = {k: v for k, v in job.items() if k != "content"}
            d = board.get("details", {}).get(job_id, DETAIL)
            if (params or {}).get("pay_transparency") == "true":
                out["pay_input_ranges"] = copy.deepcopy(d["pay_input_ranges"])
            if (params or {}).get("questions") == "true":
                out["questions"] = copy.deepcopy(d["questions"])
                for group in ("location_questions", "compliance", "demographic_questions"):
                    if group in d:
                        out[group] = copy.deepcopy(d[group])
            return out
        raise AssertionError(f"unexpected URL {url}")


def board(n=5, name="Acme", **job_kw):
    return {"name": name, "jobs": [make_job(i, **job_kw) for i in range(n)]}


class ClientFactory:
    """Stands in for ``RateLimitedClient()`` in ``src.main``: ``async with factory() as client``."""

    def __init__(self, client: FakeClient):
        self.client = client

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc):
        return None
