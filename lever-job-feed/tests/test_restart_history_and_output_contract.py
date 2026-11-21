"""Regression tests for the second round of review findings: capped restarts must reach the named history,
distinct state keys stay distinct, opening-only descriptions, primary-first locations, README shape."""

import re
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.lever import all_locations, normalize
from src.main import PENDING_KEY
from src.seen import STORE_NAME, lock_key, normalize_state_key, record_key
from tests.fakes import Platform, posting
from tests.helpers import board, go, ids

START, JOB = D("0.00005"), D("0.0015")
PRICES = {"apify-actor-start": START, "job": JOB}
BOARDS = {("global", "acme"): board(20)}
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setenv("CHARGING_ENABLED", "1")
    monkeypatch.delenv("ACTOR_RUN_ID", raising=False)


def migrated_platform(max_total="10", **kw):
    """A run that died after delivering and charging p1..p3 but before the named history was saved."""
    p = Platform(prices=dict(PRICES), max_total=D(max_total), **kw)
    p.charged["apify-actor-start"] = 1
    p.charged["job"] = 3
    p.dataset = [{"site": "acme", "postingId": f"p{i}", "recordType": None} for i in (1, 2, 3)]
    p.kv[PENDING_KEY] = {"pending": "acme|p3"}
    return p


# ---------------------------------------------------------------- P1: nothing left to do on restart
@pytest.mark.parametrize("max_items,max_total", [(3, "10"), (100, "0.00455")], ids=["maxItems-reached", "budget-spent"])
async def test_capped_restart_still_saves_recovered_rows_to_the_named_history(monkeypatch, max_items, max_total):
    p = migrated_platform(max_total)
    inp = {"sites": ["acme"], "onlyNewJobs": True, "maxItems": max_items, "descriptionFormat": "none"}
    _, lever, a = await go(monkeypatch, BOARDS, inp, platform=p)
    assert not a.failed and lever.requests == [] and p.charged["job"] == 3
    s = p.kv["SUMMARY"]
    assert s["recordsPushedThisRun"] == 0 and s["incremental"]["tracked"] == 3 and s["unbilledJobs"] == {}
    assert set(p.named[STORE_NAME][record_key("default")]["seen"]) == {"acme|p1", "acme|p2", "acme|p3"}

    # the next scheduled run shares only the named store: it must not deliver (or bill) p1..p3 again
    fresh = Platform(prices=dict(PRICES), named=p.named)
    fresh.charged["apify-actor-start"] = 1
    await go(monkeypatch, BOARDS, {**inp, "maxItems": 2}, platform=fresh)
    assert ids(fresh) == ["p4", "p5"] and fresh.charged["job"] == 2


async def test_capped_restart_keeps_unconfirmed_row_out_of_the_named_history(monkeypatch):
    p = migrated_platform()
    p.charged["job"] = 2  # p3's charge is not confirmed
    inp = {"sites": ["acme"], "onlyNewJobs": True, "maxItems": 3, "descriptionFormat": "none"}
    await go(monkeypatch, BOARDS, inp, platform=p)
    assert list(p.kv["SUMMARY"]["unbilledJobs"]) == ["acme|p3"]
    assert set(p.named[STORE_NAME][record_key("default")]["seen"]) == {"acme|p1", "acme|p2"}


# ---------------------------------------------------------------- P2: state keys are not trimmed together
def test_state_keys_differing_only_in_whitespace_have_their_own_record_and_lock():
    assert len({record_key(k) for k in ("daily", " daily ", "daily ", "\tdaily")}) == 4
    assert len({lock_key(k) for k in ("daily", " daily ")}) == 2
    assert normalize_state_key(" daily ") == " daily "
    assert normalize_state_key(None) == normalize_state_key("") == normalize_state_key("  ") == "default"


async def test_whitespace_variant_state_key_does_not_suppress_the_other_schedule(monkeypatch):
    monkeypatch.delenv("CHARGING_ENABLED")
    named = {}
    inp = {"sites": ["acme"], "onlyNewJobs": True, "maxItems": 3, "descriptionFormat": "none"}
    p1, *_ = await go(monkeypatch, BOARDS, {**inp, "stateKey": "daily"}, platform=Platform(named=named))
    p2, *_ = await go(monkeypatch, BOARDS, {**inp, "stateKey": " daily "}, platform=Platform(named=named))
    assert ids(p1) == ids(p2) == ["p1", "p2", "p3"]
    assert {record_key("daily"), record_key(" daily ")} <= set(named[STORE_NAME])
    assert p2.kv["SUMMARY"]["incremental"]["stateKey"] == " daily "


# ---------------------------------------------------------------- P2: a priced dataset-item event (see test_charging)
# ---------------------------------------------------------------- P2: opening-only description
@pytest.mark.parametrize("fmt", ["text", "html", "markdown"])
@pytest.mark.parametrize("empty_body", [{"descriptionBody": "", "descriptionBodyPlain": ""},
                                        {"descriptionBody": None, "descriptionBodyPlain": None}])
def test_opening_only_posting_does_not_repeat_the_opening_as_description(fmt, empty_body):
    raw = posting("o1", opening="<div>Join our team</div>")
    raw.update(openingPlain="Join our team", description="<div>Join our team</div>",
               descriptionPlain="Join our team", **empty_body)
    rec = normalize(raw, "acme", "global", fmt, True, "2026-10-19T00:00:00Z")
    assert rec["opening"] and "Join our team" in rec["opening"]
    assert rec["description"] is None


@pytest.mark.parametrize("fmt", ["text", "html", "markdown"])
def test_legacy_posting_without_body_fields_falls_back_to_description(fmt):
    raw = posting("o2")
    del raw["descriptionBody"], raw["descriptionBodyPlain"]
    rec = normalize(raw, "acme", "global", fmt, True, "2026-10-19T00:00:00Z")
    assert rec["description"] and "About" in rec["description"]


def test_body_html_present_without_plain_does_not_borrow_the_combined_plain_text():
    raw = posting("o3", opening="<div>Hello</div>")
    raw["descriptionPlain"] = "Hello About us"
    del raw["descriptionBodyPlain"]
    rec = normalize(raw, "acme", "global", "text", True, "2026-10-19T00:00:00Z")
    assert rec["description"] == "About us"  # converted from the body html, no opening in it


# ---------------------------------------------------------------- P3: primary location first
@pytest.mark.parametrize("location,locs,expected", [
    ("Paris", ["Lyon", "Paris"], ["Paris", "Lyon"]),
    ("Paris", ["Paris", "Lyon"], ["Paris", "Lyon"]),
    ("Paris", ["Lyon"], ["Paris", "Lyon"]),
    ("Paris", [], ["Paris"]),
    (None, ["Lyon", "Paris"], ["Lyon", "Paris"]),
    ("Paris", ["Lyon", "Paris", "Paris"], ["Paris", "Lyon"]),
])
def test_all_locations_put_the_primary_location_first_once(location, locs, expected):
    assert all_locations({"categories": {"location": location, "allLocations": locs}}) == expected


# ---------------------------------------------------------------- README / copy conventions
def test_readme_has_no_h1_only_h2_h3():
    in_fence, heads = False, []
    for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"#{1,6}\s", line):
            heads.append(line)
    assert heads and not any(h.startswith("# ") for h in heads)
    assert all(re.match(r"#{2,3} ", h) for h in heads)


def test_no_stale_prices_anywhere_in_copy():
    for path in [ROOT / "README.md", ROOT / "CHANGELOG.md", *(ROOT / ".actor").glob("*.json")]:
        text = path.read_text(encoding="utf-8")
        for bad in ("$0.001 ", "$0.001)", "$0.001,", "$0.001.", "$0.01 ", "$0.01)", "$0.01,", "$0.01."):
            assert bad not in text, (path.name, bad)
        assert not re.search(r"\$0\.0(01|1)(?!\d)", text), path.name


def test_readme_opening_leads_with_what_price_and_a_cheap_first_look():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lead = " ".join(readme.split("\n\n")[0].split())
    assert "$0.0015 per job result" in lead and "`maxItems` 20" in lead and "$0.03005" in lead


def test_readme_related_actors_section_names_only_own_actors():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Related Actors")[1].split("\n## ")[0]
    assert "`zendren/ats-job-feed`: Greenhouse, Lever, Ashby and Workday in one run" in section
    assert "`zendren/greenhouse-job-feed`: Greenhouse only, with pay ranges and the department and office hierarchy" in section
    assert set(re.findall(r"`([\w-]+/[\w-]+)`", section)) == {"zendren/ats-job-feed", "zendren/greenhouse-job-feed"}


def test_all_locations_dedupes_secondary_locations():
    from src.lever import all_locations
    raw = {"categories": {"location": "Paris", "allLocations": ["Lyon", "Paris", "Lyon"]}}
    assert all_locations(raw) == ["Paris", "Lyon"]
    assert all_locations({"categories": {"allLocations": ["A", "B", "A"]}}) == ["A", "B"]
