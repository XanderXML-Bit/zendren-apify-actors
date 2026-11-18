# README claims and capture provenance

Checked on 2026-11-06 by the maintainer.
These records cover the root README, not Store listing text.

| Claim | Source | Checked | Owner |
|---|---|---|---|
| Six actors are public under `zendren`. | Read-only `GET /v2/acts/zendren~<directory>` for every directory in the README. Each response has `isPublic: true`. | 2026-11-06 | Maintainer |
| Job feeds support the named applicant tracking systems. | `ats-job-feed/src/ats/`, `greenhouse-job-feed/src/`, and `lever-job-feed/src/`. | 2026-11-06 | Maintainer |
| PDF, sitemap, and feed transformations have the stated purposes. | Each actor's `transform.py` and offline tests. | 2026-11-06 | Maintainer |
| Job feeds use Python 3.12. Document actors use Python 3.11. | Per-actor `Dockerfile`, `.python-version`, and `.github/workflows/ci.yml`. | 2026-11-06 | Maintainer |
| The root image shows actual offline feed output. | The command below runs `feed_document` against the committed Python releases fixture. | 2026-11-06 | Maintainer |
| The capture has 10 entries, seven new entries, zero duplicates, and zero fetched URLs. | Unmodified selected fields from that command's JSON output. | 2026-11-06 | Maintainer |

## Reproduce the capture

Set up `feed-snapshot-delta/` as the root README describes.
Run this command inside that directory.

```sh
.venv/bin/python -c 'import json; from pathlib import Path; from transform import feed_document; fixture = json.loads(Path("tests/fixtures/inline_example.json").read_text()); print(json.dumps(feed_document(fixture["documents"][0]), indent=2))'
```

`docs/feed-output.png` renders selected fields and the first two entries from the resulting JSON in a headless browser.
The image labels this projection explicitly.
The full result has more entries than the image displays.
The capture uses the unchanged parser and fixture in this repository.
It does not show the Apify Console, platform storage, charging, customer activity, or earnings.
The dates and release names belong to the saved fixture, not a fresh feed fetch.
