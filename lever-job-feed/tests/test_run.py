"""Run-level behaviour against a fake Lever API (no network, charging off)."""

from datetime import datetime, timezone

import pytest

from tests.fakes import Platform, posting
from tests.helpers import board, go, ids


@pytest.fixture(autouse=True)
def no_charging(monkeypatch):
    monkeypatch.delenv("CHARGING_ENABLED", raising=False)


def S(p):
    return p.kv["SUMMARY"]


# ---------------- basics, sites, region ----------------
async def test_basic_run_pushes_normalized_records_newest_first(monkeypatch):
    p, lever, _ = await go(monkeypatch, {("global", "acme"): board(5)}, {"sites": ["acme"], "maxItems": 100})
    assert ids(p) == ["p1", "p2", "p3", "p4", "p5"]
    r = p.dataset[0]
    assert r["site"] == "acme" and r["region"] == "global" and r["workplaceType"] == "hybrid"
    assert r["lists"] == [{"heading": "What you'll do", "items": ["Build things", "Ship & learn"]}]
    assert S(p)["sites"]["acme"] == {"count": 5, "reason": "completed", "region": "global",
                                      "postingsOnBoard": 5, "matched": 5}
    assert S(p)["stoppedReason"] == "completed" and S(p)["errors"] == []


async def test_all_url_shapes_resolve_to_the_same_site(monkeypatch):
    boards = {("global", "acme"): board(2)}
    p, lever, _ = await go(monkeypatch, boards, {"sites": [
        "https://jobs.lever.co/acme/abc-123/apply?lever-source=x", "jobs.lever.co/acme", "acme"]})
    assert len(p.dataset) == 2  # three spellings, one site, no duplicates
    assert len(lever.requests) == 1


async def test_auto_region_tries_global_then_eu(monkeypatch):
    p, lever, _ = await go(monkeypatch, {("eu", "euco"): board(2)}, {"sites": ["euco"]})
    assert [r.url.host for r in lever.requests] == ["api.lever.co", "api.eu.lever.co"]
    assert {r["region"] for r in p.dataset} == {"eu"} and S(p)["sites"]["euco"]["region"] == "eu"


async def test_auto_region_uses_url_host_without_fallback(monkeypatch):
    boards = {("eu", "euco"): board(2), ("global", "gco"): board(1)}
    p, lever, _ = await go(monkeypatch, boards, {"sites": ["https://jobs.eu.lever.co/euco/x/apply", "https://jobs.lever.co/gco"]})
    assert sorted(r.url.host for r in lever.requests) == ["api.eu.lever.co", "api.lever.co"]
    # an EU URL for a site that only exists globally is an error, not silently re-routed
    p, lever, _ = await go(monkeypatch, {("global", "x"): board(1)}, {"sites": ["https://jobs.eu.lever.co/x"]})
    assert p.dataset == [] and S(p)["sites"]["x"]["reason"] == "error"
    assert [r.url.host for r in lever.requests] == ["api.eu.lever.co"]


async def test_explicit_region_overrides_url_host_and_disables_fallback(monkeypatch):
    p, lever, _ = await go(monkeypatch, {("eu", "x"): board(1)}, {"sites": ["https://jobs.lever.co/x"], "region": "eu"})
    assert len(p.dataset) == 1 and [r.url.host for r in lever.requests] == ["api.eu.lever.co"]
    p, lever, _ = await go(monkeypatch, {("eu", "x"): board(1)}, {"sites": ["x"], "region": "global"})
    assert p.dataset == [] and [r.url.host for r in lever.requests] == ["api.lever.co"]
    assert "not found" in S(p)["sites"]["x"]["error"]


async def test_error_per_site_other_sites_continue(monkeypatch):
    boards = {("global", "good"): board(3), ("global", "broken"): board(3)}
    p, _, a = await go(monkeypatch, boards, {"sites": ["good", "missing", "https://example.com/zzz", "broken", "bad name"]},
                       fail={("global", "broken"): 400})
    assert len(p.dataset) == 3 and not a.failed
    s = S(p)
    assert s["sites"]["good"]["reason"] == "completed"
    assert s["sites"]["missing"]["reason"] == "error" and "not found" in s["sites"]["missing"]["error"]
    assert s["sites"]["broken"]["reason"] == "error" and "400" in s["sites"]["broken"]["error"]
    inputs = {e["input"]: e["error"] for e in s["errors"]}
    assert "unsupported host" in inputs["https://example.com/zzz"] and "valid Lever site" in inputs["bad name"]
    assert "missing" in inputs and "broken" in inputs


async def test_no_valid_sites_fails_clearly(monkeypatch):
    p, _, a = await go(monkeypatch, {}, {"sites": ["https://example.com/x", ""]})
    assert a.failed and "No valid Lever sites" in a.failed and p.dataset == []
    assert S(p)["recordsPushed"] == 0 and "No valid Lever sites" in S(p)["error"]
    assert [e["input"] for e in S(p)["errors"]] == ["https://example.com/x"]


@pytest.mark.parametrize("bad", [{"region": "mars"}, {"descriptionFormat": "pdf"}, {"postedSince": "someday"},
                                  {"workplaceTypes": ["moon"]}, {"maxItems": "lots"}])
async def test_invalid_options_fail_before_any_request(monkeypatch, bad):
    p, lever, a = await go(monkeypatch, {("global", "acme"): board(2)}, {"sites": ["acme"], **bad})
    assert a.failed and lever.requests == [] and p.dataset == []
    assert S(p)["recordsPushed"] == 0 and S(p)["error"] == a.failed


# ---------------- pagination ----------------
async def test_pagination_reads_every_page(monkeypatch):
    p, lever, _ = await go(monkeypatch, {("global", "big"): board(250)}, {"sites": ["big"], "maxItems": 1000})
    assert len(p.dataset) == 250 and len(set(ids(p))) == 250
    skips = [int(r.url.params["skip"]) for r in lever.requests]
    assert skips == [0, 100, 200] and {r.url.params["limit"] for r in lever.requests} == {"100"}


async def test_pagination_exact_multiple_and_ignored_skip(monkeypatch):
    p, lever, _ = await go(monkeypatch, {("global", "big"): board(200)}, {"sites": ["big"], "maxItems": 1000})
    assert len(p.dataset) == 200 and len(lever.requests) == 3  # the empty third page ends it
    # a server that ignores skip must not loop forever
    import src.main as m
    from tests.fakes import FakeLever
    lever2 = FakeLever({("global", "loop"): board(100)})
    orig = lever2.handler
    def ignoring_skip(req):
        return orig(req.__class__("GET", req.url.copy_remove_param("skip")))
    import httpx
    from src.http import RateLimitedClient
    monkeypatch.setattr(m, "make_client", lambda: RateLimitedClient(transport=httpx.MockTransport(ignoring_skip), delay=0, backoff_base=0))
    from tests.fakes import FakeActor
    from src.main import run
    p2 = Platform()
    await run(FakeActor(p2, {"sites": ["loop"], "maxItems": 1000}))
    assert len(p2.dataset) == 100


# ---------------- server-side filters ----------------
async def test_server_side_filters_are_sent_as_repeated_params(monkeypatch):
    posts = board(3, team="Platform") + [posting("x1", "Other", team="Sales", department="GTM", location="Berlin",
                                                  all_locations=["Berlin", "London"], commitment="Intern")]
    inp = {"sites": ["acme"], "teams": ["platform", "SALES"], "departments": ["Engineering", "gtm"],
           "locations": ["Berlin", "London"], "commitments": ["full-time", "intern"]}
    p, lever, _ = await go(monkeypatch, {("global", "acme"): posts}, inp)
    q = lever.requests[0].url.params
    assert q.get_list("team") == ["platform", "SALES"] and q.get_list("department") == ["Engineering", "gtm"]
    assert q.get_list("location") == ["Berlin", "London"] and q.get_list("commitment") == ["full-time", "intern"]
    assert q["mode"] == "json"
    assert sorted(ids(p)) == ["p1", "p2", "p3", "x1"]
    assert S(p)["serverSideFilters"] == {"teams": ["platform", "SALES"], "departments": ["Engineering", "gtm"],
                                          "locations": ["Berlin", "London"], "commitments": ["full-time", "intern"]}


async def test_levels_filter_and_no_filters_sends_none(monkeypatch):
    posts = [posting("a", level="Senior"), posting("b", level="Junior")]
    p, lever, _ = await go(monkeypatch, {("global", "acme"): posts}, {"sites": ["acme"], "levels": ["senior"]})
    assert ids(p) == ["a"] and lever.requests[0].url.params.get_list("level") == ["senior"]
    p, lever, _ = await go(monkeypatch, {("global", "acme"): posts}, {"sites": ["acme"]})
    assert set(lever.requests[0].url.params.keys()) == {"mode", "skip", "limit"}
    assert S(p)["serverSideFilters"] == {}


async def test_location_filter_is_case_sensitive_like_lever_and_hints(monkeypatch):
    posts = board(2, location="London")
    p, _, _ = await go(monkeypatch, {("global", "acme"): posts}, {"sites": ["acme"], "locations": ["london"]})
    e = S(p)["sites"]["acme"]
    assert p.dataset == [] and e["reason"] == "no_matches" and "discoverFilters" in e["hint"]
    p, _, _ = await go(monkeypatch, {("global", "acme"): posts}, {"sites": ["acme"], "locations": ["London"]})
    assert len(p.dataset) == 2 and "hint" not in S(p)["sites"]["acme"]


# ---------------- client-side filters ----------------
async def test_client_side_filters_in_run(monkeypatch):
    sal = {"min": 1, "max": 2, "currency": "USD", "interval": "per-year-salary"}
    posts = [posting("r1", "Data Engineer", workplace="remote", salary=sal),
             posting("r2", "Data Analyst", department="Analytics", workplace="remote"),
             posting("o1", "Data Engineer", workplace="onsite", salary=sal),
             posting("u1", "Chef", team="Kitchen", department="Food", workplace="unspecified")]
    boards = {("global", "acme"): posts}
    p, *_ = await go(monkeypatch, boards, {"sites": ["acme"], "keyword": "data engineer"})
    assert sorted(ids(p)) == ["o1", "r1"]
    p, *_ = await go(monkeypatch, boards, {"sites": ["acme"], "workplaceTypes": ["remote", "unspecified"]})
    assert sorted(ids(p)) == ["r1", "r2", "u1"]
    p, *_ = await go(monkeypatch, boards, {"sites": ["acme"], "salaryOnly": True})
    assert sorted(ids(p)) == ["o1", "r1"] and p.dataset[0]["salary"]["interval"] == "year"
    assert S(p)["clientSideFilters"] == {"salaryOnly": True}
    p, *_ = await go(monkeypatch, boards, {"sites": ["acme"], "keyword": "engineer", "workplaceTypes": ["on-site"], "salaryOnly": True})
    assert ids(p) == ["o1"]


async def test_posted_since_in_run(monkeypatch):
    now = datetime.now(timezone.utc).timestamp() * 1000
    posts = [posting("new", created=now - 2 * 86400_000), posting("old", created=now - 30 * 86400_000),
             posting("nodate", created=None) | {"createdAt": None}]
    p, *_ = await go(monkeypatch, {("global", "acme"): posts}, {"sites": ["acme"], "postedSince": "7 days"})
    assert ids(p) == ["new"] and S(p)["clientSideFilters"]["postedSince"] == "7 days"
    p, *_ = await go(monkeypatch, {("global", "acme"): posts}, {"sites": ["acme"], "postedSince": "2000-01-01"})
    assert sorted(ids(p)) == ["new", "old"]


async def test_description_format_and_lists_options_in_run(monkeypatch):
    boards = {("global", "acme"): board(1)}
    p, *_ = await go(monkeypatch, boards, {"sites": ["acme"]})
    assert p.dataset[0]["description"] == "About us" and p.dataset[0]["lists"]
    p, *_ = await go(monkeypatch, boards, {"sites": ["acme"], "descriptionFormat": "none", "includeLists": False})
    r = p.dataset[0]
    assert r["description"] is None and r["opening"] is None and r["additional"] is None and r["lists"] is None
    p, *_ = await go(monkeypatch, boards, {"sites": ["acme"], "descriptionFormat": "html"})
    assert p.dataset[0]["description"] == "<div>About <b>us</b></div>"
    p, *_ = await go(monkeypatch, boards, {"sites": ["acme"], "descriptionFormat": "markdown"})
    assert p.dataset[0]["description"] == "About **us**"


# ---------------- maxItems / fair split ----------------
async def test_max_items_split_fairly_across_sites(monkeypatch):
    boards = {("global", "a"): board(50, "a"), ("global", "b"): board(50, "b"), ("global", "c"): board(2, "c")}
    p, *_ = await go(monkeypatch, boards, {"sites": ["a", "b", "c"], "maxItems": 10})
    per = {s: sum(1 for r in p.dataset if r["site"] == s) for s in "abc"}
    assert per == {"a": 4, "b": 4, "c": 2} and len(p.dataset) == 10
    s = S(p)
    assert s["sites"]["a"]["reason"] == "cap_reached" and s["sites"]["c"]["reason"] == "completed"
    assert s["stoppedReason"] == "maxItems"
    # newest postings of each site are the ones delivered
    assert [r["postingId"] for r in p.dataset if r["site"] == "a"] == ["a1", "a2", "a3", "a4"]


async def test_max_items_default_is_100(monkeypatch):
    p, *_ = await go(monkeypatch, {("global", "a"): board(150)}, {"sites": ["a"]})
    assert len(p.dataset) == 100 and S(p)["sites"]["a"]["reason"] == "cap_reached"


async def test_no_matches_reason(monkeypatch):
    p, *_ = await go(monkeypatch, {("global", "a"): board(3)}, {"sites": ["a"], "keyword": "zzz"})
    assert p.dataset == [] and S(p)["sites"]["a"] == {"count": 0, "reason": "no_matches", "region": "global",
                                                       "postingsOnBoard": 3, "matched": 0}


# ---------------- discoverFilters ----------------
async def test_discover_filters_one_record_per_site(monkeypatch):
    boards = {("global", "a"): board(3, team="T1") + board(2, "q", team="T2", workplace="remote"),
              ("eu", "b"): board(1, "b")}
    p, lever, _ = await go(monkeypatch, boards, {"sites": ["a", "b", "ghost"], "discoverFilters": True, "maxItems": 1,
                                                  "keyword": "ignored", "teams": ["T1"]})
    # one record per readable site, whatever maxItems and the job filters say
    assert [r["site"] for r in p.dataset] == ["a", "b"]
    a = p.dataset[0]
    # server-side filters still narrow what is counted (drill-down); client-side ones are ignored
    assert a["recordType"] == "filters" and a["totalPostings"] == 3 and a["teams"] == [{"value": "T1", "count": 3}]
    s = S(p)
    assert s["mode"] == "discoverFilters" and s["sites"]["ghost"]["reason"] == "error"
    assert s["sites"]["a"]["count"] == 1 and s["sites"]["a"]["reason"] == "completed"


async def test_discover_ignores_job_filters_but_counts_whole_board_without_server_filters(monkeypatch):
    boards = {("global", "a"): board(3, team="T1") + board(2, "q", team="T2", workplace="remote")}
    p, *_ = await go(monkeypatch, boards, {"sites": ["a"], "discoverFilters": True, "keyword": "zzz",
                                           "workplaceTypes": ["on-site"], "salaryOnly": True, "maxItems": 1})
    r = p.dataset[0]
    assert r["totalPostings"] == 5 and {t["value"]: t["count"] for t in r["teams"]} == {"T1": 3, "T2": 2}
    assert {w["value"]: w["count"] for w in r["workplaceTypes"]} == {"hybrid": 3, "remote": 2}
