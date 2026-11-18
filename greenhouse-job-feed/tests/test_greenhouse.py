"""Offline tests for the Greenhouse layer: hierarchy, filters, content, pay, questions, custom fields."""

from datetime import datetime, timedelta, timezone

import pytest

from src import greenhouse as gh
from src.greenhouse import JobFilter, Options, Tree, fetch_jobs
from src.models import BoardRef, GreenhouseError, HttpStatusError, RunReport
from tests.fixtures import DEPARTMENTS, DETAIL, OFFICES, FakeClient, board, make_job

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


async def collect(client, flt=None, opts=None, token="acme", **kw):
    report = kw.pop("report", None) or RunReport()
    jobs = [j async for j in fetch_jobs(client, BoardRef(token), flt or JobFilter(), opts or Options(),
                                        report=report, **kw)]
    return jobs, report


def ids(jobs):
    return sorted(j.job_id for j in jobs)


# ---------------------------------------------------------------- hierarchy
def test_tree_paths_and_ancestors():
    offices = Tree(OFFICES)
    assert offices.path(12) == ["North America", "USA", "San Francisco"]
    assert offices.chain(12) == [12, 11, 10]
    assert Tree(DEPARTMENTS).path(2) == ["Engineering", "Backend"]
    assert offices.path(999) == []


def test_tree_cycle_and_unknown_parent_are_safe():
    t = Tree([{"id": 1, "name": "A", "parent_id": 2}, {"id": 2, "name": "B", "parent_id": 1},
              {"id": 3, "name": "C", "parent_id": 77}])
    assert t.path(1) == ["B", "A"]
    assert t.path(3) == ["C"]
    assert [n["name"] for n in t.nested()] == ["C"]  # a pure cycle has no root; no crash


def test_ids_named_is_case_and_space_insensitive_and_reports_misses():
    ids_, missing = Tree(OFFICES).ids_named(["north  america", "Narnia"])
    assert ids_ == {10} and missing == ["Narnia"]


def test_nested_counts_include_descendants():
    jobs = [make_job(0, depts=(2,)), make_job(1, depts=(3,)), make_job(2, depts=(4,))]
    nested = Tree(DEPARTMENTS).nested(gh._direct_members(jobs, "departments"))
    eng = next(n for n in nested if n["name"] == "Engineering")
    assert eng["jobs"] == 2 and [c["name"] for c in eng["children"]] == ["Backend", "Frontend"]
    assert next(n for n in nested if n["name"] == "Sales")["jobs"] == 1


def test_nested_counts_a_job_once_per_subtree():
    # one job on the parent AND its child, one on both children, one on a single child
    jobs = [make_job(0, depts=(1, 2)), make_job(1, depts=(2, 3)), make_job(2, depts=(3,)), make_job(3, depts=(1,))]
    nested = Tree(DEPARTMENTS).nested(gh._direct_members(jobs, "departments"))
    eng = next(n for n in nested if n["name"] == "Engineering")
    kids = {c["name"]: c["jobs"] for c in eng["children"]}
    assert eng["jobs"] == 4 and kids == {"Backend": 2, "Frontend": 2}
    one = Tree(DEPARTMENTS).nested(gh._direct_members([make_job(0, depts=(1, 2))], "departments"))
    assert next(n for n in one if n["name"] == "Engineering")["jobs"] == 1
    offices = Tree(OFFICES).nested(gh._direct_members([make_job(0, offices=(12, 13, 11))], "offices"))
    na = next(n for n in offices if n["name"] == "North America")
    assert na["jobs"] == 1 and na["children"][0]["jobs"] == 1


async def test_department_filter_includes_child_departments():
    b = {"name": "Acme", "jobs": [make_job(0, depts=(2,)), make_job(1, depts=(3,)), make_job(2, depts=(4,)),
                                  make_job(3, depts=(1,))]}
    jobs, rep = await collect(FakeClient({"acme": b}), JobFilter(departments=["Engineering"]))
    assert ids(jobs) == [1000, 1001, 1003]          # Backend, Frontend and Engineering itself
    jobs, _ = await collect(FakeClient({"acme": b}), JobFilter(departments=["backend"]))
    assert ids(jobs) == [1000]                       # a child does not select its parent or siblings
    jobs, _ = await collect(FakeClient({"acme": b}), JobFilter(departments=["Sales", "Backend"]))
    assert ids(jobs) == [1000, 1002]


async def test_office_filter_includes_child_offices():
    b = {"name": "Acme", "jobs": [make_job(0, offices=(12,)), make_job(1, offices=(13,)),
                                  make_job(2, offices=(21,)), make_job(3, offices=(12, 21))]}
    jobs, _ = await collect(FakeClient({"acme": b}), JobFilter(offices=["North America"]))
    assert ids(jobs) == [1000, 1001, 1003]
    jobs, _ = await collect(FakeClient({"acme": b}), JobFilter(offices=["USA", "London"]))
    assert ids(jobs) == [1000, 1001, 1002, 1003]
    jobs, _ = await collect(FakeClient({"acme": b}), JobFilter(offices=["San Francisco"]))
    assert ids(jobs) == [1000, 1003]


async def test_unknown_filter_names_are_reported_not_fatal():
    c = FakeClient({"acme": {"name": "Acme", "jobs": [make_job(0)]}})
    jobs, rep = await collect(c, JobFilter(departments=["Engineering", "Marketing"]))
    assert ids(jobs) == [1000]                       # the known name still selects
    assert rep.unmatched_filters["acme"] == {"departments": ["Marketing"], "offices": []}
    jobs, rep = await collect(c, JobFilter(offices=["Mars"]))
    assert jobs == [] and rep.unmatched_filters["acme"] == {"departments": [], "offices": ["Mars"]}


async def test_paths_use_first_department_and_office():
    jobs, _ = await collect(FakeClient({"acme": {"name": "Acme", "jobs": [make_job(0, depts=(3, 4), offices=(13, 21))]}}))
    d = jobs[0].data
    assert d["departments"] == ["Frontend", "Sales"] and d["departmentPath"] == ["Engineering", "Frontend"]
    assert d["offices"] == ["New York", "London"] and d["officePath"] == ["North America", "USA", "New York"]


async def test_office_path_follows_the_first_office_in_api_order():
    """officePath is the path of offices[0] only; `offices` lists all (a live Figma job: ["Canada", "US"] -> Canada path)."""
    for order in ((13, 21), (21, 13)):
        jobs, _ = await collect(FakeClient({"acme": {"name": "Acme", "jobs": [make_job(0, offices=order)]}}))
        d = jobs[0].data
        assert len(d["offices"]) == 2 and d["officePath"][-1] == d["offices"][0]


async def test_hierarchy_endpoint_failure_degrades_but_delivers():
    client = FakeClient({"acme": {"name": "Acme", "jobs": [make_job(0, depts=(2,), offices=(12,))]}},
                        fail={"/departments": HttpStatusError(500, "x"), "/offices": GreenhouseError("down")})
    jobs, rep = await collect(client, JobFilter(departments=["Backend"]))
    assert ids(jobs) == [1000]                          # direct membership still works
    assert jobs[0].data["departmentPath"] == ["Backend"]  # parent unknown: path is the node alone
    assert "departments" in rep.hierarchy_unavailable["acme"] and "offices" in rep.hierarchy_unavailable["acme"]
    jobs, _ = await collect(client, JobFilter(departments=["Engineering"]))
    assert jobs == []                                   # cannot expand a parent we cannot see


# ---------------------------------------------------------------- filters
async def test_keyword_and_location_and_remote():
    b = {"name": "Acme", "jobs": [
        make_job(0, title="Senior Data Engineer", location="London, UK", offices=(21,)),
        make_job(1, title="Account Executive", depts=(4,), location="Remote - US", offices=(13,)),
        make_job(2, title="Staff Backend Engineer", location="Berlin", offices=(12,)),
        make_job(3, title="Designer (Remote)", location="Toronto", depts=(3,)),
    ]}
    c = FakeClient({"acme": b})
    assert ids((await collect(c, JobFilter(keyword="data engineer")))[0]) == [1000]
    assert ids((await collect(c, JobFilter(keyword="engineer sales")))[0]) == []       # words are ANDed
    assert ids((await collect(c, JobFilter(keyword="designer, sales")))[0]) == [1001, 1003]  # commas OR, department too
    assert ids((await collect(c, JobFilter(location="london")))[0]) == [1000]
    assert ids((await collect(c, JobFilter(location="new york")))[0]) == [1001]        # matches the office name
    assert ids((await collect(c, JobFilter(location="berlin, toronto")))[0]) == [1002, 1003]
    assert ids((await collect(c, JobFilter(remote_only=True)))[0]) == [1001, 1003]


def test_is_remote_heuristic():
    assert gh.is_remote("Remote - US", [], "x")
    assert gh.is_remote("Anywhere in Europe", [], "x")
    assert gh.is_remote("Berlin", ["Remote EMEA"], "x")
    assert gh.is_remote("Berlin", [], "Staff Engineer (Remote)")
    assert gh.is_remote("Berlin", [], "Eng", {"Workplace Type": "Remote"})
    assert gh.is_remote("Berlin", [], "Eng", {"Work Model": ["Remote"]})
    assert not gh.is_remote("Berlin", [], "Eng", {"Workplace Type": "Hybrid"})
    assert not gh.is_remote("Berlin", [], "Remote Sensing Scientist")
    assert not gh.is_remote("Berlin", [], "Eng", {"Notes": "remote"})
    assert not gh.is_remote(None, [], "")


@pytest.mark.parametrize("value,expected", [
    ("7 days", NOW - timedelta(days=7)),
    ("7d", NOW - timedelta(days=7)),
    ("2 weeks ago", NOW - timedelta(weeks=2)),
    ("12 hours", NOW - timedelta(hours=12)),
    ("1 month", NOW - timedelta(days=30)),
    ("2026-09-01", datetime(2026, 9, 1, tzinfo=timezone.utc)),
    ("2026-09-01T10:00:00Z", datetime(2026, 9, 1, 10, tzinfo=timezone.utc)),
    ("2026-09-01T10:00:00-04:00", datetime(2026, 9, 1, 14, tzinfo=timezone.utc)),
    ("", None), (None, None),
])
def test_parse_updated_since(value, expected):
    got = gh.parse_updated_since(value, now=NOW)
    assert (got == expected) if expected else got is None


def test_parse_updated_since_rejects_garbage():
    with pytest.raises(ValueError, match="updatedSince"):
        gh.parse_updated_since("last tuesday")


async def test_updated_since_uses_updated_at_by_default_and_first_published_on_request():
    b = {"name": "Acme", "jobs": [
        make_job(0, updated="2026-10-18T10:00:00-04:00", first_published="2026-01-01T10:00:00-04:00"),  # edited, old
        make_job(1, updated="2026-10-18T09:00:00-04:00", first_published="2026-10-17T10:00:00-04:00"),  # new
        make_job(2, updated="2026-08-01T10:00:00-04:00", first_published="2026-08-01T10:00:00-04:00"),  # old
        make_job(3, updated="2026-10-18T10:00:00-04:00", first_published=None),                          # no first publish
    ]}
    since = datetime(2026, 10, 10, tzinfo=timezone.utc)
    c = FakeClient({"acme": b})
    jobs, _ = await collect(c, JobFilter(updated_since=since))
    assert ids(jobs) == [1000, 1001, 1003]
    jobs, _ = await collect(c, JobFilter(updated_since=since, date_field="firstPublishedAt"))
    assert ids(jobs) == [1001, 1003]                      # falls back to updated_at when never published


async def test_updated_since_boundary_is_inclusive_and_timezone_aware():
    b = {"name": "Acme", "jobs": [make_job(0, updated="2026-10-10T00:00:00-04:00")]}  # = 04:00 UTC
    c = FakeClient({"acme": b})
    assert ids((await collect(c, JobFilter(updated_since=datetime(2026, 10, 10, 4, tzinfo=timezone.utc))))[0]) == [1000]
    assert ids((await collect(c, JobFilter(updated_since=datetime(2026, 10, 10, 4, 0, 1, tzinfo=timezone.utc))))[0]) == []


async def test_newest_first_order():
    b = {"name": "Acme", "jobs": [make_job(0, updated="2026-01-01T00:00:00+00:00"),
                                  make_job(1, updated="2026-03-01T00:00:00+00:00"),
                                  make_job(2, updated="2026-02-01T00:00:00+00:00")]}
    jobs, _ = await collect(FakeClient({"acme": b}))
    assert [j.job_id for j in jobs] == [1001, 1002, 1000]


# ---------------------------------------------------------------- output fields
async def test_item_fields_and_content_formats():
    c = FakeClient({"acme": {"name": "Acme Inc", "jobs": [make_job(0, location="Remote - US")]}})
    text = (await collect(c))[0][0].data
    assert list(text) == ["jobId", "internalJobId", "requisitionId", "title", "boardToken", "companyName",
                          "location", "offices", "officePath", "departments", "departmentPath", "remote",
                          "absoluteUrl", "applyUrl", "updatedAt", "firstPublishedAt", "language", "content",
                          "customFields", "scrapedAt"]
    assert text["jobId"] == 1000 and text["internalJobId"] == 5000 and text["requisitionId"] == "R0"
    assert text["boardToken"] == "acme" and text["companyName"] == "Acme" and text["remote"] is True
    assert text["absoluteUrl"].endswith("gh_jid=1000")
    assert text["applyUrl"] == "https://job-boards.greenhouse.io/acme/jobs/1000"
    assert text["updatedAt"] == "2026-10-10T10:00:00-04:00" and text["firstPublishedAt"] == "2026-09-01T10:00:00-04:00"
    assert text["language"] == "en" and text["scrapedAt"].endswith("+00:00")
    assert text["content"] == "About\n\nBuild great things & ship.\n\n- Python\n- SQL"

    html = (await collect(c, opts=Options(include_content="html")))[0][0].data["content"]
    assert html.startswith("<h2>About</h2><p>Build <strong>great</strong>")
    md = (await collect(c, opts=Options(include_content="markdown")))[0][0].data["content"]
    assert md == "## About\n\nBuild **great** things & ship.\n\n- Python\n- SQL"
    none = (await collect(c, opts=Options(include_content="none")))[0][0].data
    assert "content" not in none


async def test_company_name_falls_back_to_board_name_and_token():
    j = make_job(0)
    del j["company_name"]
    d = (await collect(FakeClient({"acme": {"name": "Board Name", "jobs": [j]}})))[0][0].data
    assert d["companyName"] == "Board Name"


async def test_missing_optional_fields_are_none_or_empty():
    j = make_job(0, depts=(), offices=())
    j.update({"requisition_id": "", "first_published": None, "location": None, "metadata": None})
    d = (await collect(FakeClient({"acme": {"name": "Acme", "jobs": [j]}})))[0][0].data
    assert d["requisitionId"] is None and d["firstPublishedAt"] is None and d["location"] is None
    assert d["offices"] == [] and d["officePath"] == [] and d["departments"] == [] and d["departmentPath"] == []
    assert d["customFields"] == {}


async def test_location_falls_back_to_first_office():
    j = make_job(0, offices=(21,))
    j["location"] = {"name": None}
    assert (await collect(FakeClient({"acme": {"name": "A", "jobs": [j]}})))[0][0].data["location"] == "London"


async def test_custom_fields_keep_types_and_can_be_switched_off():
    c = FakeClient({"acme": {"name": "Acme", "jobs": [make_job(0)]}})
    d = (await collect(c))[0][0].data
    assert d["customFields"] == {"Employment Type": "Full time", "Skills": ["Python", "SQL"], "Relocation": True}
    d = (await collect(c, opts=Options(include_custom_fields=False)))[0][0].data
    assert "customFields" not in d


# ---------------------------------------------------------------- pay ranges + questions
def test_pay_ranges_cents_to_units():
    out = gh.normalize_pay_ranges(DETAIL)
    assert out[0] == {"min": 150000, "max": 200000.5, "currency": "USD", "title": "US Zone 1", "description": "Base pay only"}
    assert out[1] == {"min": None, "max": 40, "currency": "EUR", "title": "Hourly", "description": None}
    assert isinstance(out[0]["min"], int) and isinstance(out[1]["max"], int)
    assert gh.normalize_pay_ranges({"pay_input_ranges": []}) == [] and gh.normalize_pay_ranges({}) == []
    assert gh.normalize_pay_ranges({"pay_input_ranges": [{"min_cents": 1999, "max_cents": 2001}]})[0]["min"] == 19.99


def test_questions_normalized_one_entry_per_field():
    qs = gh.normalize_questions(DETAIL)
    assert [(q["label"], q["type"], q["name"], q["required"]) for q in qs] == [
        ("First Name", "input_text", "first_name", True),
        ("Resume/CV", "input_file", "resume", True),
        ("Resume/CV", "textarea", "resume_text", True),
        ("Work authorization?", "multi_value_single_select", "question_1", False),
    ]
    assert qs[1]["description"] == "PDF please" and qs[0]["description"] is None
    assert qs[3]["options"] == [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}] and qs[0]["options"] == []


def test_location_compliance_and_demographic_groups_are_exposed():
    loc = gh.normalize_location_questions(DETAIL)
    assert [(q["label"], q["name"], q["type"], q["required"]) for q in loc] == [
        ("Location", "job_application_location", "input_text", True)]   # hidden latitude/longitude dropped
    comp = gh.normalize_compliance_questions(DETAIL)
    assert comp == [{"type": "eeoc", "description": "Voluntary self-identification", "questions": [{
        "label": "Gender", "required": False, "type": "multi_value_single_select", "name": "gender",
        "description": None, "options": [{"label": "Female", "value": 1}, {"label": "Decline", "value": 3}]}]}]
    demo = gh.normalize_demographic_questions(DETAIL)
    assert demo["header"] == "Diversity survey" and demo["description"] == "Optional"
    assert demo["questions"] == [{"id": 77, "label": "Which describes you?", "required": False,
                                  "type": "multi_value_multi_select",
                                  "options": [{"label": "A", "value": 701, "freeForm": False},
                                              {"label": "Other", "value": 702, "freeForm": True}]}]
    assert gh.normalize_location_questions({}) == [] and gh.normalize_compliance_questions({}) == []
    assert gh.normalize_demographic_questions({}) is None
    assert gh.normalize_demographic_questions({"demographic_questions": {"questions": []}}) is None


async def test_question_groups_reach_the_item_only_with_include_questions():
    jobs, _ = await collect(FakeClient({"acme": board(1)}), opts=Options(include_questions=True))
    d = jobs[0].data
    assert len(d["locationQuestions"]) == 1 and d["complianceQuestions"][0]["type"] == "eeoc"
    assert d["demographicQuestions"]["questions"][0]["id"] == 77
    jobs, _ = await collect(FakeClient({"acme": board(1)}), opts=Options(include_pay_ranges=True))
    assert not {"locationQuestions", "complianceQuestions", "demographicQuestions"} & set(jobs[0].data)


async def test_detail_requests_only_when_asked_and_with_the_right_params():
    c = FakeClient({"acme": board(2)})
    await collect(c)
    assert not [p for p, _ in c.calls if "/jobs/" in p]
    c = FakeClient({"acme": board(2)})
    jobs, _ = await collect(c, opts=Options(include_pay_ranges=True))
    details = [(p, q) for p, q in c.calls if p.rsplit("/", 2)[-2] == "jobs"]
    assert len(details) == 2 and all(q == {"pay_transparency": "true"} for _, q in details)
    assert jobs[0].data["payRanges"][0]["min"] == 150000 and "questions" not in jobs[0].data
    c = FakeClient({"acme": board(1)})
    jobs, _ = await collect(c, opts=Options(include_pay_ranges=True, include_questions=True))
    assert [q for p, q in c.calls if "/jobs/" in p] == [{"questions": "true", "pay_transparency": "true"}]
    assert len(jobs[0].data["questions"]) == 4 and jobs[0].data["scrapedAt"] == list(jobs[0].data.values())[-1]


async def test_job_without_published_pay_gets_empty_list():
    b = board(1)
    b["details"] = {1000: {"pay_input_ranges": [], "questions": [], "location_questions": []}}
    jobs, _ = await collect(FakeClient({"acme": b}), opts=Options(include_pay_ranges=True))
    assert jobs[0].data["payRanges"] == []


async def test_detail_is_fetched_only_for_jobs_that_pass_the_filters():
    b = {"name": "A", "jobs": [make_job(0, title="Keep"), make_job(1, title="Drop"), make_job(2, title="Keep too")]}
    c = FakeClient({"acme": b})
    jobs, _ = await collect(c, JobFilter(keyword="keep"), Options(include_pay_ranges=True))
    assert ids(jobs) == [1000, 1002]
    assert sorted(p.rsplit("/", 1)[-1] for p, _ in c.calls if "/jobs/" in p) == ["1000", "1002"]


async def test_detail_failure_delivers_null_pay_and_is_reported():
    c = FakeClient({"acme": board(2)}, fail={"/jobs/1000": GreenhouseError("giving up")})
    jobs, rep = await collect(c, opts=Options(include_pay_ranges=True, include_questions=True))
    by = {j.job_id: j.data for j in jobs}
    assert by[1000]["payRanges"] is None and by[1000]["questions"] is None   # unknown, not "none published"
    assert by[1000]["locationQuestions"] is None and by[1000]["complianceQuestions"] is None
    assert by[1000]["demographicQuestions"] is None
    assert by[1001]["payRanges"]
    assert rep.detail_failures == {"acme": [1000]}


async def test_detail_failure_in_incremental_mode_skips_the_job_so_it_is_retried():
    c = FakeClient({"acme": board(2)}, fail={"/jobs/1000": GreenhouseError("giving up")})
    jobs, rep = await collect(c, opts=Options(include_pay_ranges=True, only_new=True))
    assert ids(jobs) == [1001] and rep.detail_failures == {"acme": [1000]}


async def test_job_removed_between_list_and_detail_is_skipped():
    c = FakeClient({"acme": board(2)}, fail={"/jobs/1001": HttpStatusError(404, "x")})
    jobs, rep = await collect(c, opts=Options(include_pay_ranges=True))
    assert ids(jobs) == [1000] and rep.detail_failures == {}


async def test_detail_requests_run_concurrently_but_results_stay_ordered():
    import asyncio
    active = peak = 0

    class Slow(FakeClient):
        async def get_json(self, url, params=None):
            nonlocal active, peak
            if "/jobs/" in url:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01 * (5 - int(url.rsplit("/", 1)[-1]) % 5))
                active -= 1
            return await super().get_json(url, params)

    jobs, _ = await collect(Slow({"acme": board(10)}), opts=Options(include_pay_ranges=True))
    assert 1 < peak <= gh.DETAIL_WINDOW
    assert [j.job_id for j in jobs] == [j.job_id for j in sorted(jobs, key=lambda j: j.job_id)] and len(jobs) == 10


# ---------------------------------------------------------------- board errors / bookkeeping
async def test_unknown_board_is_a_clear_error():
    with pytest.raises(GreenhouseError, match="not found"):
        await collect(FakeClient({}), token="nope")


async def test_report_counts_and_trees():
    c = FakeClient({"acme": {"name": "Acme Inc", "jobs": [make_job(0), make_job(1, title="Other")]}})
    _, rep = await collect(c, JobFilter(keyword="job 0"))
    assert rep.names == {"acme": "Acme Inc"} and rep.on_board == {"acme": 2} and rep.matched == {"acme": 1}
    tree = rep.trees["acme"]
    assert tree["departments"][0]["name"] == "Engineering" and tree["departments"][0]["jobs"] == 2
    assert next(o for o in tree["offices"] if o["name"] == "North America")["children"][0]["name"] == "USA"


async def test_seen_jobs_are_skipped_and_known_ones_counted_and_touched():
    c = FakeClient({"acme": board(4)})
    touched = []
    jobs, rep = await collect(c, seen={"acme|1000", "acme|1001"}, known={"acme|1001", "acme|1003"},
                              touch=touched.append)
    assert ids(jobs) == [1002, 1003]
    assert sorted(touched) == ["acme|1001", "acme|1003"]      # every listed tracked job is refreshed
    assert rep.already_seen == {"acme": 1}                      # 1000 was delivered by this run, not "known"

