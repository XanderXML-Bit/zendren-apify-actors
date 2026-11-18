# Public PDF to JSON with Page-Cited RAG Chunks

Convert text-based PDFs — from a **public URL** or pasted as base64 — into clean, **page-cited JSON** and **deterministic RAG chunks** ready for a vector database or an AI agent — without installing PDF libraries or paying for an LLM.

## Why use it
- **Citations built in** — every chunk carries its `page` plus exact `start`/`end` character offsets, so answers can cite the page.
- **Deterministic** — the same PDF and settings always give byte-identical chunks (stable IDs for upserts), with a SHA-256 of the input.
- **Predictable cost** — one flat price per document, no per-token or per-page surprises.
- **Safe by design** — isolated parser, strict size limits, no network access.

Typical uses: RAG ingestion of public reports, filings, manuals and papers; agent tools that need "read this PDF" with citations; deduplication by hash.

## Input
| Field | Description |
|---|---|
| `publicDataOnly` | Must be `true` (see Responsible use). |
| `pdfUrls[]` | Public `http(s)` URLs of PDFs to fetch (see *Fetching from URLs*). |
| `chunkSize`, `chunkOverlap` | Optional defaults for URL PDFs: chunk size 250–4000 chars (default 1500), overlap 0–1000 (default 150, less than chunkSize). |
| `documents[]` | Optional pasted PDFs: objects with `pdfBase64` (required, base64 of the PDF) and optional per-document `chunkSize` / `chunkOverlap`. |

Supply at least one of `pdfUrls` or `documents`; **1–5 items in total per run** (URLs + pasted documents).

Limits: 1 MiB decoded PDF, 1–20 pages, 200,000 text characters. Encrypted, malformed and image-only (scanned) PDFs are rejected uncharged — there is no OCR, table or image extraction. JSON input under 8 MB, output under 1 MB per document. Fixed 256 MB memory. Each document is parsed in an isolated child process (no Apify credentials, 160 MiB memory, 15 s CPU / 20 s wall-time limits). Nothing in the input is executed.

Example input (a public W3C test PDF URL) is prefilled in Console and in `INPUT.example.json`:
```json
{"publicDataOnly": true, "pdfUrls": ["https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"]}
```

### Fetching from URLs: safety and limits
The Actor downloads only what you list, from the main process; the parser still runs in an isolated, network-free child process.
- Public `http`/`https` URLs only, ports 80 and 443 only, no credentials in the URL (`user:pass@`).
- The hostname is resolved and **every** resulting address must be publicly routable. Localhost, private (10/8, 172.16/12, 192.168/16), link-local/cloud-metadata (169.254/16), CGNAT (100.64/10), multicast, reserved and IPv6 loopback/ULA/site-local addresses are refused (`URL_BLOCKED`). The connection is pinned to the vetted address, so DNS rebinding cannot redirect it.
- Up to 3 redirects, each hop re-validated the same way. 10 s connect timeout, 20 s total per URL. No cookies, no authentication headers, no proxies.
- Responses are capped at 1 MiB (gzip transfer encoding accepted; the decompressed size is capped too, and the download is aborted once the cap is exceeded).
- Failed fetches (`URL_BLOCKED`, `FETCH_FAILED`, `FETCH_TIMEOUT`, `TOO_LARGE`, `WRONG_CONTENT`) are listed in `OUTPUT.errors` with `documentIndex`, the error code and the URL, and are **never charged**. Only successfully processed items are charged.
- `documentIndex` counts pasted `documents` first, then URL entries. Every output item has `sourceUrl` (`null` for pasted documents) and `fetchedUrls`; `OUTPUT.fetchedUrls` is the number of successful downloads in the run.
- Requests identify as `ZendrenApifyActor/0.2 (+https://apify.com/zendren)`. Only fetch content you are permitted to process.

## Output
One dataset item per processed PDF:
```json
{"documentIndex": 0, "kind": "pdf", "sha256": "3df79d34…", "pageCount": 1, "characterCount": 14,
 "sourceUrl": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf", "fetchedUrls": 1,
 "pages": [{"page": 1, "text": "Dummy PDF file"}],
 "chunks": [{"page": 1, "start": 0, "end": 14, "text": "Dummy PDF file"}]}
```
The key-value store record `OUTPUT` summarises the run: `processed` and `fetchedUrls` counts, an `errors` list (with `documentIndex`, error code, message and, for URL items, the `url`) and `stoppedForSpendingLimit`. Invalid documents are reported there, are **not** written to the dataset and are **not** charged; valid siblings still process. Check `OUTPUT` even when the run status is *Succeeded*.

## Pricing

Pay per event, no subscription, no platform-usage pass-through:

| Event | Price | When |
|---|---|---|
| `processed-document` | **$0.008** | One PDF successfully processed and saved to the dataset |
| `apify-actor-start` | $0.00005 | Once per run (Apify synthetic start event) |

Example: 5 documents in one run = $0.04005. Invalid documents are free; the start event still applies to runs whose input is rejected. Set **Maximum cost per run** in Console or `?maxTotalChargeUsd=` in the API — the Actor stops before parsing a document it cannot pay for.

## Use it from the API, Make, n8n or an AI agent

```bash
curl -X POST "https://api.apify.com/v2/acts/zendren~public-pdf-json-chunks/run-sync-get-dataset-items?maxTotalChargeUsd=0.05" \
  -H "Authorization: Bearer $APIFY_TOKEN" -H "Content-Type: application/json" \
  -d @INPUT.example.json
```

Send the token in the `Authorization` header, never in the URL. The Actor is also usable as an MCP tool through Apify's MCP server (`mcp.apify.com`).

## Responsible use

Supply only public data you are permitted to process, with no personal data. `publicDataOnly` is a policy acknowledgement, not an automated privacy detector. The Actor only downloads the exact public URLs you list (no crawling, no logins, no proxies) and never calls external AI services.

## Support

Found a bug or need a format supported? Open an issue on the Actor's **Issues** tab. Fixture attributions: see `NOTICE.md` in the source.
