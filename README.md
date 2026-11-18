# Apify actors

Python actors for developers who need public job feeds, page-cited PDF chunks, sitemap comparisons, or feed change detection.

## Choose an actor

All six actors have public Apify Store pages, checked on 2026-11-06.
This repository is the public source for those actors.

| Directory | Public actor | Purpose |
|---|---|---|
| `ats-job-feed/` | [ATS job feed](https://apify.com/zendren/ats-job-feed) | Combine Greenhouse, Lever, Ashby, and Workday job feeds. |
| `greenhouse-job-feed/` | [Greenhouse job feed](https://apify.com/zendren/greenhouse-job-feed) | Read public Greenhouse job boards. |
| `lever-job-feed/` | [Lever job feed](https://apify.com/zendren/lever-job-feed) | Read public Lever job postings. |
| `public-pdf-json-chunks/` | [Public PDF JSON chunks](https://apify.com/zendren/public-pdf-json-chunks) | Extract text and chunks with page references from text-based PDFs. |
| `sitemap-snapshot-audit/` | [Sitemap snapshot audit](https://apify.com/zendren/sitemap-snapshot-audit) | Compare current and previous sitemap XML documents. |
| `feed-snapshot-delta/` | [Feed snapshot delta](https://apify.com/zendren/feed-snapshot-delta) | Parse RSS or Atom feeds and identify previously unseen entries. |

Each actor's README documents its input, output, limits, and pricing.
Check its Store page before a platform run, which can incur charges.

## Inspect real output

![Actual offline feed transformation output, including entry and new-item counts, from the committed Python releases fixture.](docs/feed-output.png)

The image shows the real feed parser's output from the committed fixture, not a live Store run.
It uses no token, makes no network request, and incurs no platform charge.
[Capture provenance](docs/claims.md) records its source and reproduction command.

## Check the stack

The job-feed actors use Python 3.12, the Apify SDK, and HTTPX.
The document actors use Python 3.11, the Apify SDK, HTTPX, defusedxml, and pypdf.
Pytest runs the offline suites.
Pytest-asyncio runs asynchronous job-feed tests with function-scoped fixture loops.
Each directory contains a runtime pin and a hash-locked development dependency file.
The lockfiles fix test environments, not the existing Docker production dependency resolution.

## Set up a local actor

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) before these steps.
The commands below create an isolated environment for one actor.

1. Clone the repository.

   ```sh
   git clone https://github.com/XanderXML-Bit/zendren-apify-actors.git
   cd zendren-apify-actors
   ```

2. Enter an actor directory.

   ```sh
   cd feed-snapshot-delta
   ```

3. Create its pinned Python environment.

   ```sh
   uv venv --python "$(cat .python-version)" .venv
   uv pip sync --python .venv/bin/python requirements-dev.lock
   ```

4. Run its offline tests and compile check.

   ```sh
   .venv/bin/python -m pytest -q -m "not live" -p no:cacheprovider
   .venv/bin/python -m compileall -q -x '/[.]venv/' .
   ```

Repeat steps two through four for each actor, starting from the repository root.
Run each suite inside its actor directory because actors use separate module names.
The `live` marker excludes tests that contact public job boards.
Some parser tests skip when an optional tool is unavailable.

## Run the offline feed example

From `feed-snapshot-delta/`, run the real parser against its committed fixture.

```sh
.venv/bin/python -c 'import json; from pathlib import Path; from transform import feed_document; fixture = json.loads(Path("tests/fixtures/inline_example.json").read_text()); print(json.dumps(feed_document(fixture["documents"][0]), indent=2))'
```

This command runs the feed transformation only.
It does not emulate Apify storage or platform charging.

## Deployment

Deployment to the Apify platform is maintained separately and is not part of this repository.
Do not run platform commands during offline setup, and keep tokens outside the repository.
The [CI workflow](.github/workflows/ci.yml) runs offline tests and dependency audits on main pushes and pull requests.

## Brand

Brand: Zendren. Created by [Xander M. Lopez](https://xandermlopez.com).
The Apify username is `zendren`.

## License

Copyright (c) 2026 Xander M. Lopez. All rights reserved.
See [LICENSE](LICENSE) for the proprietary terms.
Third-party dependency and fixture licenses remain separate.
