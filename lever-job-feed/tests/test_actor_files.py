"""The .actor files agree with each other and with the code."""

import json
import re
from pathlib import Path


from src.lever import DESCRIPTION_FORMATS, WORKPLACE_TYPES, discover, normalize
from src.main import DEFAULT_MAX_ITEMS, JOB_EVENT
from src.sites import REGIONS, parse_site
from tests.fakes import posting

ROOT = Path(__file__).resolve().parent.parent
ACTOR = ROOT / ".actor"


def load(name):
    return json.loads((ACTOR / name).read_text())


def test_actor_json_limits_and_wiring():
    a = load("actor.json")
    assert a["name"] == "lever-job-feed" and a["version"] == "0.1"
    assert len(a["title"]) <= 50 and len(a["description"]) <= 300
    assert a["defaultMemoryMbytes"] in (512, 1024) and a["minMemoryMbytes"] == 256
    assert a["environmentVariables"] == {"CHARGING_ENABLED": "1"}
    for key in ("input", "output"):
        assert (ACTOR / a[key]).exists()
    assert (ACTOR / a["storages"]["dataset"]).exists()
    assert (ACTOR / a["readme"]).resolve() == ROOT / "README.md"
    assert (ACTOR / a["changelog"]).resolve() == ROOT / "CHANGELOG.md" and (ROOT / "CHANGELOG.md").exists()
    assert (ACTOR / a["dockerfile"]).resolve() == ROOT / "Dockerfile"
    assert (ROOT / "Dockerfile").read_text().startswith("FROM apify/actor-python:3.12")


def test_pay_per_event_reference():
    ppe = load("pay_per_event.json")
    assert ppe["job"]["eventPriceUsd"] == 0.0015 and ppe["job"]["eventTitle"] == "Job result"
    assert ppe["job"]["isPrimaryEvent"] is True and JOB_EVENT == "job"
    assert ppe["apify-actor-start"]["eventPriceUsd"] == 0.00005
    assert "apify-default-dataset-item" not in ppe


def test_input_schema_matches_code():
    s = load("input_schema.json")
    props = s["properties"]
    assert s["required"] == ["sites"]
    assert props["maxItems"]["default"] == DEFAULT_MAX_ITEMS
    assert props["region"]["enum"] == list(REGIONS)
    assert props["descriptionFormat"]["enum"] == list(DESCRIPTION_FORMATS) and props["descriptionFormat"]["default"] == "text"
    assert props["includeLists"]["default"] is True and props["discoverFilters"]["default"] is False
    assert props["onlyNewJobs"]["default"] is False
    assert props["workplaceTypes"]["items"]["enum"] == list(WORKPLACE_TYPES)
    for name, p in props.items():
        assert p["title"] and p["description"], name
        if "enumTitles" in p:
            assert len(p["enumTitles"]) == len(p["enum"])
    for entry in props["sites"]["prefill"]:
        assert parse_site(entry)
    # every option is read by the Actor
    src = (ROOT / "src" / "main.py").read_text() + (ROOT / "src" / "lever.py").read_text()
    for name in props:
        assert re.search(rf'["\']{name}["\']', src), f"input field {name!r} is never read"


def test_dataset_schema_documents_every_output_field_and_views_use_known_fields():
    d = load("dataset_schema.json")
    documented = set(d["fields"]["properties"])
    job = normalize(posting(1), "acme", "global", "text", True, "ts")
    assert set(job) <= documented, set(job) - documented
    assert set(discover([posting(1)], "acme", "global", "ts")) <= documented
    assert "overview" in d["views"]
    for name, view in d["views"].items():
        fields = view["transformation"]["fields"]
        assert set(fields) <= documented, (name, set(fields) - documented)
        assert set(view["display"]["properties"]) == set(fields)
    # output keys are camelCase
    assert all(re.fullmatch(r"[a-z][A-Za-z]*", k) for k in documented)


def test_output_schema_links_to_existing_views():
    out = load("output_schema.json")
    views = load("dataset_schema.json")["views"]
    for p in out["properties"].values():
        m = re.search(r"view=(\w+)", p["template"])
        if m:
            assert m.group(1) in views


def test_requirements_and_pytest_ini():
    assert "apify" in (ROOT / "requirements.txt").read_text()
    assert "live" in (ROOT / "pytest.ini").read_text()


def test_readme_copy_rules_and_required_sections():
    readme = (ROOT / "README.md").read_text()
    texts = {"README": readme}
    for f in ("actor.json", "input_schema.json", "dataset_schema.json", "output_schema.json", "pay_per_event.json"):
        texts[f] = (ACTOR / f).read_text()
    for name, t in texts.items():
        assert not re.search(r"xander|lopez", t, re.I), name
        # the username may only appear inside actor ids
        for m in re.finditer(r"zendren", t, re.I):
            assert re.match(r"zendren[/~](lever|ats|greenhouse)-job-feed", t[m.start():m.start() + 30]), (name, t[m.start() - 20:m.start() + 30])
    assert '"url": "https://mcp.apify.com/?tools=fetch-actor-details,zendren/lever-job-feed"' in readme
    assert '"type": "http"' in readme and "state the price before the first paid run" in readme
    assert "maxTotalChargeUsd=" in readme and "Authorization: Bearer $APIFY_TOKEN" in readme
    assert "token=" not in readme.replace("tokens", "")
    for fragment in ("$0.03005", "$0.15005", "$1.50005", "$0.0015", "$0.00005", "## FAQ", "## Pricing",
                     "## Use with AI agents (MCP)", "jobs.eu.lever.co", "lever-job-feed-state"):
        assert fragment in readme, fragment
    faq = readme.split("## FAQ")[1].split("## Limits")[0]
    assert len(re.findall(r"^\*\*.+\?\*\*", faq, re.M)) >= 8
    assert "not an MCP server itself" in readme


def test_review_copy_pricing_mcp_and_limits():
    readme = (ROOT / "README.md").read_text()
    assert "at the default memory" in readme and "exactly one** start event" in readme
    assert "512 MB" in readme and "1024 MB" in readme
    for fragment in ("get-dataset-items", "`maxItems` is part of the Actor input", "maximum total charge is a run option",
                     "one per site record", "200,000", "stateEvicted", "LOCK-STATE", "STATE-<readable part",
                     "Possible under-billing", "unbilledJobs"):
        assert fragment in readme, fragment
    schema = json.loads((ACTOR / "input_schema.json").read_text())["properties"]
    assert "per site record" in schema["discoverFilters"]["description"]
    assert "200,000" in schema["onlyNewJobs"]["description"]
    ppe = json.loads((ACTOR / "pay_per_event.json").read_text())
    assert "per GB" in ppe["apify-actor-start"]["eventDescription"]
    assert "apify-default-dataset-item" not in ppe


def test_no_comments_about_other_platforms_left():
    for f in (ROOT / "src").glob("*.py"):
        text = f.read_text()
        assert not re.search(r"ats-job-feed|greenhouse|ashby|workday", text, re.I), f.name
