"""Live tests against the real public Lever API (a few requests).

Run with:  pytest -m live        Skip offline with:  pytest -m "not live"
"""

from __future__ import annotations

import pytest

from src.http import LeverError, RateLimitedClient
from src.lever import ServerFilters, discover, fetch_site, normalize
from src.sites import SiteRef

pytestmark = pytest.mark.live

NO_FILTERS = ServerFilters()


@pytest.fixture
async def client():
    async with RateLimitedClient() as c:
        yield c


def norm(raw, site, region):
    return normalize(raw, site, region, "text", True, "2026-10-19T00:00:00Z")


@pytest.mark.parametrize("site", ["spotify", "zoox"])
async def test_global_sites_return_valid_records(client, site):
    region, raw = await fetch_site(client, SiteRef(site), "auto", NO_FILTERS)
    assert region == "global" and len(raw) > 5
    for p in raw[:5]:
        r = norm(p, site, region)
        assert r["postingId"] and r["title"] and r["hostedUrl"].startswith("https://jobs.lever.co/" + site)
        assert r["workplaceType"] in ("remote", "hybrid", "on-site", "unspecified")
        assert r["createdAt"].endswith("Z") and isinstance(r["allLocations"], list)
        assert r["lists"] and all(s["items"] for s in r["lists"][:1])


async def test_eu_site_auto_fallback(client):
    region, raw = await fetch_site(client, SiteRef("pnlfin"), "auto", NO_FILTERS)  # not on the global instance
    assert region == "eu" and raw
    assert raw[0]["hostedUrl"].startswith("https://jobs.eu.lever.co/pnlfin/")


async def test_salary_published_by_zoox(client):
    _, raw = await fetch_site(client, SiteRef("zoox"), "global", NO_FILTERS)
    with_salary = [norm(p, "zoox", "global") for p in raw if "salaryRange" in p]
    assert with_salary, "zoox is expected to publish salary ranges"
    s = with_salary[0]["salary"]
    assert s["currency"] and s["interval"] in ("year", "hour", "month", "week", "day") and (s["min"] or s["max"])
    assert with_salary[0]["salaryText"]


async def test_server_side_filters_and_discovery_agree(client):
    region, raw = await fetch_site(client, SiteRef("spotify"), "global", NO_FILTERS)
    d = discover(raw, "spotify", region, "ts")
    assert d["totalPostings"] == len(raw) and d["teams"] and d["locations"]
    team = d["teams"][0]
    _, by_team = await fetch_site(client, SiteRef("spotify"), "global", ServerFilters(teams=[team["value"]]))
    assert len(by_team) == team["count"]
    loc = d["locations"][0]
    _, by_loc = await fetch_site(client, SiteRef("spotify"), "global", ServerFilters(locations=[loc["value"]]))
    assert len(by_loc) == loc["count"]  # location counts every location of a posting, like Lever's filter
    both = ServerFilters(teams=[d["teams"][0]["value"], d["teams"][1]["value"]])
    _, either = await fetch_site(client, SiteRef("spotify"), "global", both)
    assert len(either) == d["teams"][0]["count"] + d["teams"][1]["count"]  # repeated parameter = OR


async def test_unknown_site_is_a_clear_error(client):
    with pytest.raises(LeverError, match="not found"):
        await fetch_site(client, SiteRef("zzzz-no-such-lever-site-9"), "auto", NO_FILTERS)


async def test_pagination_covers_a_big_board(client):
    _, raw = await fetch_site(client, SiteRef("palantir"), "global", NO_FILTERS)  # 300+ postings = several pages
    assert len(raw) > 200 and len({p["id"] for p in raw}) == len(raw)
