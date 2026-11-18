"""Live smoke tests against the public Greenhouse Job Board API (a few requests each).

Run with:  python -m pytest -q -m live
"""

import pytest

from src.greenhouse import JobFilter, Options, fetch_jobs, load_board
from src.http import RateLimitedClient
from src.main import run
from src.models import BoardRef, GreenhouseError
from tests.fakes import FakeActor, Platform

pytestmark = pytest.mark.live


async def collect(client, token, flt=None, opts=None, limit=None):
    out = []
    async for job in fetch_jobs(client, BoardRef(token), flt or JobFilter(), opts or Options()):
        out.append(job)
        if limit and len(out) >= limit:
            break
    return out


async def test_two_boards_return_normalized_jobs():
    async with RateLimitedClient() as client:
        for token, name in (("figma", "Figma"), ("dropbox", "Dropbox")):
            jobs = await collect(client, token, limit=5)
            assert jobs, token
            d = jobs[0].data
            assert d["boardToken"] == token and d["companyName"] == name
            assert isinstance(d["jobId"], int) and d["title"] and d["absoluteUrl"].startswith("http")
            assert d["applyUrl"] == f"https://job-boards.greenhouse.io/{token}/jobs/{d['jobId']}"
            assert d["updatedAt"] and isinstance(d["remote"], bool) and isinstance(d["customFields"], dict)
            assert d["content"] and "<" not in d["content"] and "&lt;" not in d["content"]


async def test_board_hierarchy_is_loaded_and_paths_are_built():
    async with RateLimitedClient() as client:
        board = await load_board(client, BoardRef("dropbox"))
        assert board.name == "Dropbox" and board.raw_jobs
        assert board.departments.names and board.offices.names
        jobs = await collect(client, "dropbox")
        assert any(len(j.data["departmentPath"]) > 1 for j in jobs), "expected at least one nested department"
        # selecting a parent department returns all jobs below it
        top = next(j.data["departmentPath"][0] for j in jobs if len(j.data["departmentPath"]) > 1)
        under = await collect(client, "dropbox", JobFilter(departments=[top]))
        assert under and all(top in j.data["departmentPath"] or top in j.data["departments"] for j in under)


async def test_pay_ranges_and_questions_on_a_board_that_publishes_them():
    async with RateLimitedClient() as client:
        jobs = await collect(client, "dropbox", opts=Options(include_pay_ranges=True, include_questions=True), limit=8)
        with_pay = [j for j in jobs if j.data["payRanges"]]
        assert with_pay, "dropbox is expected to publish pay ranges"
        r = with_pay[0].data["payRanges"][0]
        assert r["currency"] and r["max"] is not None and 1000 < r["max"] < 10_000_000  # units, not cents
        qs = jobs[0].data["questions"]
        assert qs and {"label", "required", "type", "options"} <= set(qs[0])
        assert any(q["type"] == "input_file" for q in qs)


async def test_unknown_board_is_reported():
    async with RateLimitedClient() as client:
        with pytest.raises(GreenhouseError, match="not found"):
            await collect(client, "this-board-does-not-exist-zq")


async def test_full_run_over_two_boards_with_the_fake_actor():
    p = Platform()
    a = FakeActor(p, {"boards": ["figma", "https://job-boards.greenhouse.io/dropbox", "no-such-board-zq9"],
                      "maxItems": 6, "includePayRanges": True, "includeContent": "markdown"})
    await run(a)
    s = p.kv["SUMMARY"]
    assert len(p.dataset) == 6 and s["boards"]["figma"]["count"] == 3 and s["boards"]["dropbox"]["count"] == 3
    assert s["boards"]["no-such-board-zq9"]["reason"] == "error"
    assert set(p.kv["DEPARTMENTS_OFFICES"]) >= {"figma", "dropbox"}
