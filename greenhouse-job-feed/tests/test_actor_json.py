"""The .actor files agree with each other and with the code."""

import json
import re
from pathlib import Path


from src.greenhouse import CONTENT_MODES, DATE_FIELDS, Options, Tree, build_item
from src.refs import parse_board
from tests.fixtures import make_job

ROOT = Path(__file__).resolve().parent.parent
ACTOR = ROOT / ".actor"


def load(name):
    return json.loads((ACTOR / name).read_text())


def test_actor_json_basics():
    a = load("actor.json")
    assert a["name"] == "greenhouse-job-feed" and a["version"] == "0.1"
    assert len(a["title"]) <= 50 and len(a["description"]) <= 300
    assert a["environmentVariables"] == {"CHARGING_ENABLED": "1"}
    assert a["minMemoryMbytes"] == 256 and a["defaultMemoryMbytes"] in (512, 1024)
    assert a["minMemoryMbytes"] <= a["defaultMemoryMbytes"] <= a["maxMemoryMbytes"]
    assert a["maxMemoryMbytes"] == 1024   # one apify-actor-start event per run (one per GB)
    for key in ("input", "output", "dockerfile", "readme"):
        assert (ACTOR / a[key]).resolve().exists(), key
    assert a["storages"]["dataset"] == "./dataset_schema.json"
    assert (ROOT / "Dockerfile").read_text().startswith("FROM apify/actor-python:3.12")


def test_pay_per_event_file_matches_the_charging_code():
    from src.main import JOB_EVENT
    ppe = load("pay_per_event.json")
    assert set(ppe) == {"apify-actor-start", "job"} and JOB_EVENT == "job"
    assert ppe["apify-actor-start"]["eventPriceUsd"] == 0.00005
    assert ppe["job"]["eventPriceUsd"] == 0.0015 and ppe["job"]["eventTitle"] == "Job result"
    assert "apify-default-dataset-item" not in ppe


def test_input_schema_is_consistent():
    s = load("input_schema.json")
    props = s["properties"]
    assert s["required"] == ["boards"]
    assert set(props) == {"boards", "departments", "offices", "keyword", "location", "remoteOnly", "updatedSince",
                          "updatedSinceField", "includeContent", "includePayRanges", "includeQuestions",
                          "includeCustomFields", "onlyNewJobs", "stateKey", "maxItems"}
    for name, p in props.items():
        assert p["title"] and len(p["description"]) > 20, name
    assert tuple(props["includeContent"]["enum"]) == CONTENT_MODES and props["includeContent"]["default"] == "text"
    assert tuple(props["updatedSinceField"]["enum"]) == DATE_FIELDS
    assert props["maxItems"]["default"] == 100
    assert props["includePayRanges"]["default"] is False and props["includeQuestions"]["default"] is False
    assert props["includeCustomFields"]["default"] is True and props["onlyNewJobs"]["default"] is False
    for entry in props["boards"]["prefill"]:
        parse_board(entry)
    for key in ("title", "description"):
        assert "Xander" not in json.dumps(s)


def test_main_reads_exactly_the_documented_inputs():
    src = (ROOT / "src" / "main.py").read_text()
    used = set(re.findall(r'inp\.get\("(\w+)"', src))
    assert used == set(load("input_schema.json")["properties"])


def test_dataset_schema_documents_every_output_field():
    fields = set(load("dataset_schema.json")["fields"]["properties"])
    opts = Options(include_content="text", include_pay_ranges=True, include_questions=True)
    item = build_item(make_job(0), "acme", "Acme", opts, Tree(), Tree(), "2026-10-19T00:00:00+00:00")
    item["payRanges"] = []
    item["questions"] = []
    item["locationQuestions"] = item["complianceQuestions"] = item["demographicQuestions"] = None
    assert set(item) <= fields
    assert fields - set(item) == set()
    views = load("dataset_schema.json")["views"]
    assert "overview" in views
    for view in views.values():
        assert set(view["transformation"]["fields"]) <= fields
        assert set(view["display"]["properties"]) == set(view["transformation"]["fields"])


def test_output_schema_links():
    props = load("output_schema.json")["properties"]
    assert {"results", "summary", "departmentsOffices"} <= set(props)
    assert props["summary"]["template"].endswith("/records/SUMMARY")
    assert props["departmentsOffices"]["template"].endswith("/records/DEPARTMENTS_OFFICES")


def test_public_text_has_no_personal_names_or_off_platform_promotion():
    files = [ROOT / "README.md", *ACTOR.glob("*.json")]
    for f in files:
        text = f.read_text()
        for word in ("Xander", "Lopez"):
            assert word not in text, (f.name, word)
        # the Apify username may appear only inside actor ids
        for m in re.finditer(r"(?i)zendren", text):
            assert text[max(0, m.start() - 1)] == "/" or text[m.end():m.end() + 1] in ("/", "~"), (f.name, text[m.start() - 20:m.end() + 20])


def test_readme_has_no_h1_and_store_conventions():
    text = (ROOT / "README.md").read_text()
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        assert in_fence or not re.match(r"# ", line), line     # the Store renders the Actor name as H1
    assert re.findall(r"(?m)^## Related Actors$", text)
    intro = text.split("\n## ", 1)[0]
    assert "$0.0015 per job" in intro and "$0.03005" in intro and "maxItems" in intro
    assert "https://mcp.apify.com/?tools=fetch-actor-details,zendren/greenhouse-job-feed" in text
    assert "get-dataset-items" in text and "maxTotalChargeUsd" in text
    assert "Authorization: Bearer" in text or '"Authorization: Bearer' in text
    assert "?token=" not in text and "&token=<YOUR" not in text    # the API token never goes into a URL
    related = text.split("## Related Actors", 1)[1]
    assert "zendren/ats-job-feed" in related and "zendren/lever-job-feed" in related
    for word in ("Make", "n8n", "Zapier", "Google Sheets"):
        assert word in text, word
    assert "first office" in text and "`offices`" in text      # officePath semantics are documented


def test_no_old_price_strings_in_public_copy():
    for f in [ROOT / "README.md", ROOT / "CHANGELOG.md", *ACTOR.glob("*.json")]:
        assert not re.search(r"\$0\.001(?!\d)|\$0\.01(?!\d)", f.read_text()), f.name


def test_changelog_is_wired():
    a = load("actor.json")
    assert a["changelog"] == "../CHANGELOG.md" and a["readme"] == "../README.md"
    changelog = (ACTOR / a["changelog"]).resolve()
    assert changelog == (ROOT / "CHANGELOG.md").resolve() and changelog.exists()
    assert "2026-10-19" in changelog.read_text()
