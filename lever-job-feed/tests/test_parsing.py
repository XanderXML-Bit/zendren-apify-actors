"""Site/URL parsing, text conversion, filters, salary, lists, discovery (pure functions)."""

from datetime import datetime, timezone

import pytest

from src.lever import (
    ClientFilters, ServerFilters, discover, normalize, normalize_salary, normalize_workplace,
    normalize_workplace_types, parse_lists, parse_posted_since, salary_text,
)
from src.plan import fair_split
from src.sites import SiteRef, parse_site, split_entries
from src.text import html_to_markdown, html_to_text, list_items
from tests.fakes import posting


# ---------------- site / URL parsing ----------------
@pytest.mark.parametrize("entry,expected", [
    ("spotify", SiteRef("spotify")),
    ("  spotify  ", SiteRef("spotify")),
    ("my-site_1.x", SiteRef("my-site_1.x")),
    ("jobs.lever.co/spotify", SiteRef("spotify", "global")),
    ("https://jobs.lever.co/spotify", SiteRef("spotify", "global")),
    ("https://jobs.lever.co/spotify/", SiteRef("spotify", "global")),
    ("http://www.jobs.lever.co/spotify", SiteRef("spotify", "global")),
    ("https://jobs.lever.co/spotify?team=Platform&lever-source=x", SiteRef("spotify", "global")),
    ("https://jobs.lever.co/spotify#top", SiteRef("spotify", "global")),
    ("https://jobs.lever.co/spotify/223f9722-3579-4ff2-b123-e4cc7b0c1a64", SiteRef("spotify", "global")),
    ("https://jobs.lever.co/spotify/223f9722-3579-4ff2-b123-e4cc7b0c1a64/apply", SiteRef("spotify", "global")),
    ("https://jobs.lever.co/spotify/223f9722-3579-4ff2-b123-e4cc7b0c1a64/apply?lever-source=LinkedIn", SiteRef("spotify", "global")),
    ("JOBS.LEVER.CO/spotify", SiteRef("spotify", "global")),
    ("https://jobs.eu.lever.co/pnlfin", SiteRef("pnlfin", "eu")),
    ("jobs.eu.lever.co/pnlfin/2776275b-c47b-4672-8774-288a49a084c2/apply", SiteRef("pnlfin", "eu")),
    ("https://api.lever.co/v0/postings/spotify?mode=json", SiteRef("spotify", "global")),
    ("https://api.eu.lever.co/v0/postings/pnlfin/2776275b-c47b-4672-8774-288a49a084c2", SiteRef("pnlfin", "eu")),
])
def test_parse_site_shapes(entry, expected):
    assert parse_site(entry) == expected


@pytest.mark.parametrize("entry,fragment", [
    ("", "empty"),
    ("   ", "empty"),
    ("https://boards.greenhouse.io/stripe", "unsupported host"),
    ("https://example.com/spotify", "unsupported host"),
    ("https://jobs.lever.co/", "no site name"),
    ("jobs.lever.co", "no site name"),
    ("https://api.lever.co/v1/other", "no site name"),
    ("bad name", "valid Lever site"),
    ("-leading", "valid Lever site"),
    ("a/b/c", "unsupported host"),
])
def test_parse_site_errors(entry, fragment):
    with pytest.raises(ValueError, match=fragment):
        parse_site(entry)


def test_split_entries_accepts_strings_lines_and_objects():
    assert split_entries(["a, b", "c\nd", {"url": "https://jobs.lever.co/e"}, {"site": "f"}, 5, ""]) == \
        ["a", "b", "c", "d", "https://jobs.lever.co/e", "f"]


# ---------------- server-side filter query building ----------------
def test_server_filters_params_repeat_values_and_skip_empty():
    f = ServerFilters.from_input({"teams": ["A", " B ", "A", ""], "departments": ["Eng"], "locations": ["Berlin, Germany"],
                                  "commitments": [], "levels": ["Senior"]})
    assert f.teams == ["A", "B"]
    assert f.params() == {"team": ["A", "B"], "department": ["Eng"], "location": ["Berlin, Germany"],
                          "level": ["Senior"]}
    assert f.applied() == {"teams": ["A", "B"], "departments": ["Eng"], "locations": ["Berlin, Germany"],
                           "levels": ["Senior"]}
    assert ServerFilters.from_input({}).params() == {}


# ---------------- workplace type ----------------
@pytest.mark.parametrize("raw,norm", [("onsite", "on-site"), ("on-site", "on-site"), ("On Site", "on-site"),
                                       ("on_site", "on-site"), ("remote", "remote"), ("HYBRID", "hybrid"),
                                       ("unspecified", "unspecified"), (None, "unspecified"), ("", "unspecified"),
                                       ("weird", "unspecified")])
def test_normalize_workplace(raw, norm):
    assert normalize_workplace(raw) == norm


def test_workplace_types_input_validation():
    assert normalize_workplace_types(["onsite", "Remote"]) == {"on-site", "remote"}
    with pytest.raises(ValueError, match="unknown workplace type"):
        normalize_workplace_types(["moon"])


def test_workplace_filter():
    posts = [posting(1, workplace="remote"), posting(2, workplace="onsite"), posting(3, workplace="hybrid"),
             posting(4, workplace="unspecified"), posting(5, workplace=None)]
    def ids(types):
        f = ClientFilters(workplace_types=normalize_workplace_types(types))
        return [p["id"] for p in posts if f.match(p)]
    assert ids(["remote"]) == ["1"]
    assert ids(["on-site", "hybrid"]) == ["2", "3"]
    assert ids(["unspecified"]) == ["4", "5"]
    assert ids([]) == ["1", "2", "3", "4", "5"]


# ---------------- keyword ----------------
def test_keyword_matches_title_team_department():
    p = posting(1, "Senior Data Engineer", team="Data Platform", department="Engineering")
    q = posting(2, "Designer", team="Brand", department="Design")
    def m(kw, post): return ClientFilters(keyword=kw).match(post)
    assert m("data engineer", p) and m("DATA", p) and m("brand", q) and m("engineering", p)
    assert not m("data engineer", q)
    assert m("designer, engineer", p) and m("designer, engineer", q)  # comma = OR
    assert not m("data designer", p)  # words AND
    assert m(None, q) and m("  ", q)


# ---------------- postedSince ----------------
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)

@pytest.mark.parametrize("value,expected", [
    ("7 days", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)),
    ("7d", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)),
    ("2 weeks ago", datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)),
    ("36 hours", datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)),
    ("1 month", datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)),
    ("2026-10-01", datetime(2026, 10, 1, tzinfo=timezone.utc)),
    ("2026-10-01T08:30:00Z", datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)),
    ("2026-10-01T08:30:00+02:00", datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)),
])
def test_parse_posted_since(value, expected):
    assert parse_posted_since(value, now=NOW) == expected


def test_parse_posted_since_empty_and_invalid():
    assert parse_posted_since(None) is None and parse_posted_since("  ") is None
    with pytest.raises(ValueError, match="postedSince"):
        parse_posted_since("soonish")


def test_posted_since_filter_uses_created_at():
    cutoff = parse_posted_since("2026-10-10")
    ms = lambda d: int(datetime(2026, 10, d, tzinfo=timezone.utc).timestamp() * 1000)  # noqa: E731
    f = ClientFilters(posted_since=cutoff)
    assert f.match(posting(1, created=ms(10))) and f.match(posting(2, created=ms(15)))
    assert not f.match(posting(3, created=ms(9)))
    assert not f.match(posting(4, created=None) | {"createdAt": None})


# ---------------- salary ----------------
def test_salary_normalization_and_text():
    p = posting(1, salary={"min": 123000, "max": 168000.0, "currency": "usd", "interval": "per-year-salary"})
    s = normalize_salary(p)
    assert s == {"min": 123000, "max": 168000, "currency": "USD", "interval": "year"}
    assert salary_text(s) == "USD 123,000–168,000 per year"
    for raw, expect in [("per-hour-wage", "hour"), ("per-month-salary", "month"), ("per-week-wage", "week"),
                        ("per-day-wage", "day"), ("per-bi-weekly-salary", "biweekly"), ("one-time", "one-time")]:
        assert normalize_salary(posting(1, salary={"min": 1, "max": 2, "currency": "EUR", "interval": raw}))["interval"] == expect
    assert salary_text({"min": 50, "max": None, "currency": "EUR", "interval": "hour"}) == "EUR from 50 per hour"
    assert salary_text({"min": None, "max": 9, "currency": None, "interval": None}) == "up to 9"
    assert salary_text({"min": 5, "max": 5, "currency": "USD", "interval": "year"}) == "USD 5 per year"
    assert salary_text(None) is None


@pytest.mark.parametrize("sr", [None, {}, {"min": None, "max": None, "currency": "USD"}, "x", {"min": "10"}])
def test_salary_absent_or_unusable(sr):
    p = posting(1)
    if sr is not None:
        p["salaryRange"] = sr
    assert normalize_salary(p) is None


def test_salary_only_filter():
    with_salary = posting(1, salary={"min": 1, "max": 2, "currency": "USD", "interval": "per-year-salary"})
    f = ClientFilters(salary_only=True)
    assert f.match(with_salary) and not f.match(posting(2))
    assert ClientFilters().match(posting(2))


# ---------------- text / lists ----------------
def test_html_to_text_and_markdown():
    h = '<h3>Intro</h3><div>Hello <strong>bold</strong> &amp; <a href="https://x.io/a">link</a></div><ul><li>One</li><li>Two</li></ul><ol><li>A</li><li>B</li></ol>'
    t = html_to_text(h)
    assert "Hello bold & link" in t and "- One" in t and "- Two" in t and "<" not in t
    md = html_to_markdown(h)
    assert "### Intro" in md and "**bold**" in md and "[link](https://x.io/a)" in md and "- One" in md and "1. A" in md and "2. B" in md
    assert html_to_text(None) == "" and html_to_markdown("") == ""


def test_list_items_li_nested_and_fallback():
    assert list_items("<div>\n\n<li>One &amp; two</li>\n<li><b>Three</b>\n  four</li></div>") == ["One & two", "Three four"]
    assert list_items("<ul><li>A<ul><li>B</li></ul></li></ul>") == ["A B"]
    assert list_items("<div>Line one</div><div>Line two</div>") == ["Line one", "Line two"]
    assert list_items("") == [] and list_items(None) == []
    assert list_items("<li></li><li> </li>") == []


def test_parse_lists_sections():
    p = posting(1, lists=[{"text": " What you'll   do ", "content": "<li>A</li><li>B</li>"},
                          {"text": "Who you are", "content": "<li>C</li>"}, {"text": "", "content": ""}, "junk"])
    assert parse_lists(p) == [{"heading": "What you'll do", "items": ["A", "B"]},
                              {"heading": "Who you are", "items": ["C"]}]


# ---------------- description formats / normalize ----------------
def _norm(p, fmt="text", lists=True):
    return normalize(p, "acme", "global", fmt, lists, "2026-10-19T00:00:00Z")


def test_description_formats():
    p = posting(1, opening="<div>Hi <i>there</i></div>", description="<h2>About</h2><div>We <b>build</b></div>",
                additional="<div>Equal <a href='https://e.org'>opportunity</a></div>")
    p["openingPlain"], p["descriptionBodyPlain"], p["additionalPlain"] = "Hi there", "About\nWe build", "Equal opportunity"
    t = _norm(p, "text")
    assert (t["opening"], t["description"], t["additional"]) == ("Hi there", "About\nWe build", "Equal opportunity")
    h = _norm(p, "html")
    assert h["description"] == "<h2>About</h2><div>We <b>build</b></div>" and h["opening"].startswith("<div>Hi")
    m = _norm(p, "markdown")
    assert m["description"].startswith("## About")
    assert "**build**" in m["description"] and "[opportunity](https://e.org)" in m["additional"] and "*there*" in m["opening"]
    n = _norm(p, "none")
    assert n["opening"] is n["description"] is n["additional"] is None and n["salaryDescription"] is None
    assert n["lists"]  # lists are independent of descriptionFormat


def test_text_format_falls_back_to_html_conversion_and_empty_sections_are_none():
    p = posting(1, description="<div>Only <b>html</b></div>", opening="")
    p["descriptionBodyPlain"] = ""
    p["descriptionPlain"] = ""
    r = _norm(p)
    assert r["description"] == "Only html" and r["opening"] is None


def test_include_lists_false():
    assert _norm(posting(1), lists=False)["lists"] is None
    assert _norm(posting(1))["lists"] == [{"heading": "What you'll do", "items": ["Build things", "Ship & learn"]}]


def test_normalized_record_fields():
    p = posting("abc", "  Staff Engineer ", team="Core", department="Eng", location="Berlin",
                all_locations=["Berlin", "Remote - EU"], commitment=None, level="Senior", workplace="onsite",
                country="DE", created=1791566108174,
                salary={"min": 90000, "max": 120000, "currency": "EUR", "interval": "per-year-salary"},
                salary_desc="Base pay range.")
    r = _norm(p)
    assert r["postingId"] == "abc" and r["title"] == "Staff Engineer" and r["site"] == "acme" and r["region"] == "global"
    assert (r["team"], r["department"], r["location"], r["country"], r["level"]) == ("Core", "Eng", "Berlin", "DE", "Senior")
    assert r["allLocations"] == ["Berlin", "Remote - EU"] and r["commitment"] is None
    assert r["workplaceType"] == "on-site" and r["remote"] is False
    assert r["salary"] == {"min": 90000, "max": 120000, "currency": "EUR", "interval": "year"}
    assert r["salaryText"] == "EUR 90,000–120,000 per year" and r["salaryDescription"] == "Base pay range."
    assert r["createdAt"] == "2026-10-09T17:15:08.174Z"
    assert r["hostedUrl"].endswith("/abc") and r["applyUrl"].endswith("/apply") and r["scrapedAt"]
    assert _norm(posting(2, workplace="remote"))["remote"] is True
    assert _norm(posting(3))["salary"] is None and _norm(posting(3))["salaryText"] is None


def test_all_locations_includes_primary_location():
    p = posting(1, location="Paris", all_locations=["Lyon"])
    assert _norm(p)["allLocations"] == ["Paris", "Lyon"]


# ---------------- discoverFilters record ----------------
def test_discover_counts():
    posts = [posting(1, team="A", department="X", location="London", all_locations=["London", "Berlin"], workplace="remote"),
             posting(2, team="A", department="Y", location="Berlin", commitment="Contract", workplace="onsite"),
             posting(3, team="B", department="X", location="Berlin", commitment=None, level="Senior", workplace=None)]
    d = discover(posts, "acme", "eu", "ts")
    assert d["recordType"] == "filters" and d["site"] == "acme" and d["region"] == "eu" and d["totalPostings"] == 3
    assert d["teams"] == [{"value": "A", "count": 2}, {"value": "B", "count": 1}]
    assert d["departments"] == [{"value": "X", "count": 2}, {"value": "Y", "count": 1}]
    assert d["locations"] == [{"value": "Berlin", "count": 3}, {"value": "London", "count": 1}]
    assert d["commitments"] == [{"value": "Contract", "count": 1}, {"value": "Full-time", "count": 1}]
    assert d["levels"] == [{"value": "Senior", "count": 1}]
    assert d["workplaceTypes"] == [{"value": "on-site", "count": 1}, {"value": "remote", "count": 1},
                                   {"value": "unspecified", "count": 1}]


# ---------------- fair split ----------------
@pytest.mark.parametrize("cap,total,expected", [
    ([100, 100], 10, [5, 5]),
    ([100, 100, 100], 10, [4, 3, 3]),
    ([2, 100], 10, [2, 8]),
    ([2, 3, 100], 20, [2, 3, 15]),
    ([5, 5], 100, [5, 5]),
    ([0, 7], 5, [0, 5]),
    ([10, 10, 10], 2, [1, 1, 0]),
    ([], 5, []),
    ([4, 4], 0, [0, 0]),
])
def test_fair_split(cap, total, expected):
    out = fair_split(cap, total)
    assert out == expected
    assert sum(out) == min(total, sum(cap)) and all(o <= c for o, c in zip(out, cap))
