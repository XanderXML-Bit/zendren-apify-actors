import re
import json
from pathlib import Path

ACTOR_JSON = Path(__file__).resolve().parent.parent / ".actor" / "actor.json"


def test_actor_json_enables_charging():
    data = json.loads(ACTOR_JSON.read_text(encoding="utf-8"))
    assert data["environmentVariables"]["CHARGING_ENABLED"] == "1"


ACTOR_DIR = ACTOR_JSON.parent
ROOT = ACTOR_DIR.parent


def _load(name):
    return json.loads((ACTOR_DIR / name).read_text(encoding="utf-8"))


def test_pay_per_event_matches_current_pricing():
    events = _load("pay_per_event.json")
    assert set(events) == {"apify-actor-start", "job-scraped"}  # no custom actor-start any more
    assert events["apify-actor-start"]["eventPriceUsd"] == 0.00005
    assert events["job-scraped"]["eventPriceUsd"] == 0.0015
    assert events["job-scraped"]["isPrimaryEvent"] is True
    assert not events["apify-actor-start"].get("isPrimaryEvent")


def test_actor_defaults_and_store_text():
    data = _load("actor.json")
    assert data["name"] == "ats-job-feed"
    assert data["title"] == "Greenhouse, Lever, Ashby, Workday Jobs Scraper API"
    assert 40 <= len(data["title"]) <= 50
    assert data["defaultMemoryMbytes"] == 1024
    assert data["minMemoryMbytes"] <= data["defaultMemoryMbytes"] <= data["maxMemoryMbytes"]
    assert len(data["description"]) <= 300 and "$0.0015" in data["description"]
    assert "new jobs only" in data["description"].lower()


def test_input_schema_has_incremental_fields():
    props = _load("input_schema.json")["properties"]
    assert props["onlyNewJobs"]["type"] == "boolean" and props["onlyNewJobs"]["default"] is False
    assert props["stateKey"]["type"] == "string" and props["stateKey"]["editor"] == "textfield"
    assert list(props).index("onlyNewJobs") < list(props).index("maxItems")


def test_docs_have_no_old_pricing_or_personal_names():
    store = (ROOT / "STORE_README.md").read_text(encoding="utf-8")
    dev = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "$0.01" not in store and "$0.003" not in store
    assert "$0.003" not in dev and "$0.0015" in dev  # the developer readme mentions the old $0.01 only as legacy
    assert "$0.0015" in store and "$0.00005" in store
    for name, text in (("README.md", dev), ("STORE_README.md", store)):
        for word in ("Xander", "Lopez", "Zendren"):
            assert word not in text, (name, word)


def test_changelog_is_wired_and_exists():
    data = _load("actor.json")
    assert data["changelog"] == "../CHANGELOG.md"
    assert (ACTOR_DIR / data["changelog"]).resolve().is_file()
    assert (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").count("\n## 20") >= 2


def test_store_readme_has_no_h1_and_no_old_prices():
    store = (ROOT / "STORE_README.md").read_text(encoding="utf-8")
    in_fence = False
    for line in store.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        assert in_fence or not line.startswith("# "), line  # the Store shows the Actor name as H1
    for old in ("$0.01", "$0.003", "$0.001"):
        # "$0.0015" starts with "$0.001", so only match when no further digit follows
        assert not re.search(re.escape(old) + r"(?!\d)", store), old
    schema = (ACTOR_DIR / "input_schema.json").read_text(encoding="utf-8")
    meta = (ACTOR_DIR / "actor.json").read_text(encoding="utf-8")
    for text in (schema, meta):
        for old in ("$0.01", "$0.003", "$0.001"):
            assert not re.search(re.escape(old) + r"(?!\d)", text), old
