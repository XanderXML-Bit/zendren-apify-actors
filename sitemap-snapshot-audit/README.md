# Sitemap Diff – Added, Removed & Changed URLs to JSON

Give it a public `sitemap.xml` URL — or paste the XML — (optionally with a previous snapshot to diff against) and get a clean JSON audit: every URL with `lastmod`/`changefreq`/`priority`, plus **added**, **removed** and **changed** URLs, duplicates and warnings.

## Why use it
- **Change monitoring** — spot new pages, deleted pages and content updates on competitor or your own sites (schedule runs and diff against your last archived snapshot URL or pasted XML).
- **SEO QA** — catch duplicate URLs, invalid priorities and malformed entries before Google does.
- **Cheap and deterministic** — flat per-sitemap price, sorted unique output, SHA-256 per snapshot.
- **No crawling** — fetches only the sitemap files you list (no link following, no proxies).

## Input
| Field | Description |
|---|---|
| `publicDataOnly` | Must be `true`. |
| `sitemapUrls[]` | Public sitemap locations to fetch: plain URL strings or objects `{"url": "https://…", "previousUrl": "https://…"}` (`previousUrl` optional: an earlier snapshot of the same type to diff against). |
| `documents[]` | Optional pasted snapshots: objects with `xml` (required, current sitemap) and optional `previousXml`. |

Supply at least one of `sitemapUrls` or `documents`; **1–5 items in total per run** (URLs + pasted documents).

Supports standard `urlset` and `sitemapindex` (child sitemaps are listed, never fetched or recursed into). 1 MiB and 5,000 entries per snapshot. Compressed `.xml.gz` files are not unpacked; no DTDs/entities. Without `previousXml`, every URL is reported as added. JSON input under 8 MB, output under 1 MB per document. Fixed 256 MB memory. Each document is parsed in an isolated child process (no Apify credentials, 160 MiB memory, 15 s CPU / 20 s wall-time limits). Nothing in the input is executed.

Example input (the public sitemaps.org sitemap) is prefilled in Console and in `INPUT.example.json`:
```json
{"publicDataOnly": true, "sitemapUrls": [{"url": "https://www.sitemaps.org/sitemap.xml"}]}
```

### Fetching from URLs: safety and limits
The Actor downloads only what you list, from the main process; the parser still runs in an isolated, network-free child process.
- Public `http`/`https` URLs only, ports 80 and 443 only, no credentials in the URL (`user:pass@`).
- The hostname is resolved and **every** resulting address must be publicly routable. Localhost, private (10/8, 172.16/12, 192.168/16), link-local/cloud-metadata (169.254/16), CGNAT (100.64/10), multicast, reserved and IPv6 loopback/ULA/site-local addresses are refused (`URL_BLOCKED`). The connection is pinned to the vetted address, so DNS rebinding cannot redirect it.
- Up to 3 redirects, each hop re-validated the same way. 10 s connect timeout, 20 s total per URL. No cookies, no authentication headers, no proxies.
- Responses are capped at 1 MiB per sitemap file (gzip transfer encoding accepted; the decompressed size is capped too, and the download is aborted once the cap is exceeded).
- Failed fetches (`URL_BLOCKED`, `FETCH_FAILED`, `FETCH_TIMEOUT`, `TOO_LARGE`, `WRONG_CONTENT`) are listed in `OUTPUT.errors` with `documentIndex`, the error code and the URL, and are **never charged**. Only successfully processed items are charged.
- `documentIndex` counts pasted `documents` first, then URL entries. Every output item has `sourceUrl` (`null` for pasted documents) and `fetchedUrls`; `OUTPUT.fetchedUrls` is the number of successful downloads in the run.
- Requests identify as `ZendrenApifyActor/0.2 (+https://apify.com/zendren)`. Only fetch content you are permitted to process.

## Output
```json
{"documentIndex": 0, "kind": "sitemap", "type": "urlset", "entryCount": 3, "comparedToPrevious": true,
 "sourceUrl": "https://www.sitemaps.org/sitemap.xml", "previousSourceUrl": null, "fetchedUrls": 1,
 "added": ["https://www.example.com/blog/launch"], "removed": ["https://www.example.com/old-page"],
 "changed": ["https://www.example.com/pricing"], "duplicates": [], "warnings": [],
 "entries": [{"url": "https://www.example.com/", "lastmod": "2026-10-15", "changefreq": null, "priority": 1.0}], "sha256": "3721ab5e…"}
```
The key-value store record `OUTPUT` summarises the run: `processed` and `fetchedUrls` counts, an `errors` list (with `documentIndex`, error code, message and, for URL items, the `url`) and `stoppedForSpendingLimit`. Invalid documents are reported there, are **not** written to the dataset and are **not** charged; valid siblings still process. Check `OUTPUT` even when the run status is *Succeeded*.

## Pricing

Pay per event, no subscription, no platform-usage pass-through:

| Event | Price | When |
|---|---|---|
| `processed-document` | **$0.005** | One sitemap snapshot (or pair) successfully processed and saved to the dataset |
| `apify-actor-start` | $0.00005 | Once per run (Apify synthetic start event) |

Example: 5 documents in one run = $0.02505. Invalid documents are free; the start event still applies to runs whose input is rejected. Set **Maximum cost per run** in Console or `?maxTotalChargeUsd=` in the API — the Actor stops before parsing a document it cannot pay for.

## Use it from the API, Make, n8n or an AI agent

```bash
curl -X POST "https://api.apify.com/v2/acts/zendren~sitemap-snapshot-audit/run-sync-get-dataset-items?maxTotalChargeUsd=0.05" \
  -H "Authorization: Bearer $APIFY_TOKEN" -H "Content-Type: application/json" \
  -d @INPUT.example.json
```

Send the token in the `Authorization` header, never in the URL. The Actor is also usable as an MCP tool through Apify's MCP server (`mcp.apify.com`).

## Responsible use

Supply only public data you are permitted to process, with no personal data. `publicDataOnly` is a policy acknowledgement, not an automated privacy detector. The Actor only downloads the exact public URLs you list (no crawling, no logins, no proxies) and never calls external AI services.

## Support

Found a bug or need a format supported? Open an issue on the Actor's **Issues** tab. Fixture attributions: see `NOTICE.md` in the source.
