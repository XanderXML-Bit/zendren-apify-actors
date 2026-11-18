# RSS & Atom Feed to JSON with New-Item Detection

Turn an RSS 2.0 or Atom feed — from a **public URL** or pasted XML — into normalized, agent-ready JSON — stable `id`, `title`, `url`, `publishedAt` — and flag which items are **new** compared to the IDs you already processed.

## Why use it
- **"What's new?" in one call** — pass `knownIds` from your last run (top-level for URL feeds, or per pasted document); each entry gets `isNew`, plus `newCount` and `duplicateCount`.
- **One schema for RSS and Atom** — no more per-feed parsing quirks in your Make/n8n/agent workflow.
- **Deduplicated & deterministic** — duplicate GUIDs/links collapsed, SHA-256 per snapshot.
- **Cheap** — flat $0.003 per feed, ideal for frequent schedules.

## Input
| Field | Description |
|---|---|
| `publicDataOnly` | Must be `true`. |
| `feedUrls[]` | Public `http(s)` feed URLs to fetch (see *Fetching from URLs*). |
| `knownIds[]` | Optional, up to 5,000 previously seen entry IDs; applies to every feed in `feedUrls`. |
| `documents[]` | Optional pasted feeds: objects with `xml` (required, feed XML) and optional per-document `knownIds`. |

Supply at least one of `feedUrls` or `documents`; **1–5 items in total per run** (URLs + pasted documents).

RSS 2.0 and Atom only, 1 MiB and 500 entries per feed; each entry needs an absolute http(s) link. No RSS 1.0/RDF, DTDs or article fetching (only the feed document itself is downloaded). Full descriptions and author fields are intentionally omitted. JSON input under 8 MB, output under 1 MB per document. Fixed 256 MB memory. Each document is parsed in an isolated child process (no Apify credentials, 160 MiB memory, 15 s CPU / 20 s wall-time limits). Nothing in the input is executed.

Example input (the public Python releases feed) is prefilled in Console and in `INPUT.example.json`:
```json
{"publicDataOnly": true, "feedUrls": ["https://www.python.org/downloads/feed.rss"]}
```

### Fetching from URLs: safety and limits
The Actor downloads only what you list, from the main process; the parser still runs in an isolated, network-free child process.
- Public `http`/`https` URLs only, ports 80 and 443 only, no credentials in the URL (`user:pass@`).
- The hostname is resolved and **every** resulting address must be publicly routable. Localhost, private (10/8, 172.16/12, 192.168/16), link-local/cloud-metadata (169.254/16), CGNAT (100.64/10), multicast, reserved and IPv6 loopback/ULA/site-local addresses are refused (`URL_BLOCKED`). The connection is pinned to the vetted address, so DNS rebinding cannot redirect it.
- Up to 3 redirects, each hop re-validated the same way. 10 s connect timeout, 20 s total per URL. No cookies, no authentication headers, no proxies.
- Responses are capped at 1 MiB per feed (gzip transfer encoding accepted; the decompressed size is capped too, and the download is aborted once the cap is exceeded).
- Failed fetches (`URL_BLOCKED`, `FETCH_FAILED`, `FETCH_TIMEOUT`, `TOO_LARGE`, `WRONG_CONTENT`) are listed in `OUTPUT.errors` with `documentIndex`, the error code and the URL, and are **never charged**. Only successfully processed items are charged.
- `documentIndex` counts pasted `documents` first, then URL entries. Every output item has `sourceUrl` (`null` for pasted documents) and `fetchedUrls`; `OUTPUT.fetchedUrls` is the number of successful downloads in the run.
- Requests identify as `ZendrenApifyActor/0.2 (+https://apify.com/zendren)`. Only fetch content you are permitted to process.

## Output
```json
{"documentIndex": 0, "kind": "feed", "type": "rss2", "entryCount": 10, "newCount": 7, "duplicateCount": 0,
 "sourceUrl": "https://www.python.org/downloads/feed.rss", "fetchedUrls": 1,
 "entries": [{"id": "https://www.python.org/downloads/release/python-3150/", "title": "Python 3.15.0",
   "url": "https://www.python.org/downloads/release/python-3150/", "publishedAt": "Fri, 09 Oct 2026 13:54:59 +0000", "isNew": true}],
 "sha256": "483235f5…"}
```
The key-value store record `OUTPUT` summarises the run: `processed` and `fetchedUrls` counts, an `errors` list (with `documentIndex`, error code, message and, for URL items, the `url`) and `stoppedForSpendingLimit`. Invalid documents are reported there, are **not** written to the dataset and are **not** charged; valid siblings still process. Check `OUTPUT` even when the run status is *Succeeded*.

## Pricing

Pay per event, no subscription, no platform-usage pass-through:

| Event | Price | When |
|---|---|---|
| `processed-document` | **$0.003** | One feed successfully processed and saved to the dataset |
| `apify-actor-start` | $0.00005 | Once per run (Apify synthetic start event) |

Example: 5 documents in one run = $0.01505. Invalid documents are free; the start event still applies to runs whose input is rejected. Set **Maximum cost per run** in Console or `?maxTotalChargeUsd=` in the API — the Actor stops before parsing a document it cannot pay for.

## Use it from the API, Make, n8n or an AI agent

```bash
curl -X POST "https://api.apify.com/v2/acts/zendren~feed-snapshot-delta/run-sync-get-dataset-items?maxTotalChargeUsd=0.05" \
  -H "Authorization: Bearer $APIFY_TOKEN" -H "Content-Type: application/json" \
  -d @INPUT.example.json
```

Send the token in the `Authorization` header, never in the URL. The Actor is also usable as an MCP tool through Apify's MCP server (`mcp.apify.com`).

## Responsible use

Supply only public data you are permitted to process, with no personal data. `publicDataOnly` is a policy acknowledgement, not an automated privacy detector. The Actor only downloads the exact public URLs you list (no crawling, no logins, no proxies) and never calls external AI services.

## Support

Found a bug or need a format supported? Open an issue on the Actor's **Issues** tab. Fixture attributions: see `NOTICE.md` in the source.
