Lever Job Feed returns structured job postings from any company that hires through **Lever** (`jobs.lever.co/<site>` and `jobs.eu.lever.co/<site>`), with filters that run on Lever's side, salary ranges, workplace type and requirement lists. It costs **$0.0015 per job result** plus a $0.00005 start fee per run. For a cheap first look, run it with `maxItems` 20: that costs **$0.03005** in total.

The Actor reads Lever's public Postings API, so there is no browser, no HTML parsing and no login. It is built only for Lever, which lets it use what Lever offers: server-side filters, salary ranges, workplace type, requirement lists as arrays, the EU instance, and a mode that returns only postings you have not seen before. Paste a few company names, get one record per posting.

## What you get

- **Server-side filters.** Team, department, location, commitment and level are sent to Lever, so postings you do not want are never downloaded.
- **Filter discovery.** One switch lists a company's exact team, department, location and commitment values with posting counts, so you can fill the filters correctly.
- **Salary ranges** as numbers (`min`, `max`, `currency`, `interval`) plus a readable `salaryText`, and a `salaryOnly` switch.
- **Workplace type** (`remote`, `hybrid`, `on-site`, `unspecified`) and a ready-made `remote` flag.
- **Both Lever regions.** Global (`jobs.lever.co`) and EU (`jobs.eu.lever.co`) sites work in the same run. `region: auto` finds the right one.
- **Requirement lists as arrays.** "What you'll do" and "Who you are" sections come as `[{heading, items[]}]` with plain-text items, ready for matching or scoring.
- **Description as text, HTML or Markdown**, or switched off for lean output.
- **Daily feed mode.** `onlyNewJobs` delivers only postings that earlier runs did not, so a scheduled run is a stream of fresh postings.
- **Fair `maxItems` split** across all the sites you list, newest postings first.

## How to find a company's Lever site name

The site name is the last part of the company's Lever job board address:

- `https://jobs.lever.co/spotify` → site `spotify`
- `https://jobs.eu.lever.co/pnlfin` → site `pnlfin` (EU instance)

Open the company's careers page and click a job. If the address bar or the Apply button points to `jobs.lever.co/...` or `jobs.eu.lever.co/...`, the part right after the host is the site name. You can also paste the full URL, including a posting URL or an `/apply` URL: the Actor reduces it to its site. A site that does not exist (or whose company moved to another system) is reported as an error for that site, and the other sites still run.

## Input

| Field | What it does |
|---|---|
| `sites` (required) | Site names or any Lever URL. |
| `region` | `auto` (default): use the URL's host, otherwise try the global instance and fall back to EU on 404. Or force `global` / `eu`. |
| `teams`, `departments`, `locations`, `commitments`, `levels` | Server-side filters with exact Lever values. Several values in one field = OR, different fields = AND. |
| `keyword` | Words in title, team or department (all words must match; commas separate alternatives). |
| `workplaceTypes` | Any of `remote`, `hybrid`, `on-site`, `unspecified`. |
| `postedSince` | A date (`2026-10-01`) or relative value (`7 days`, `2 weeks`, `36 hours`, `1 month`), compared with the posting's creation time. |
| `salaryOnly` | Only postings that publish a salary range. |
| `descriptionFormat` | `none`, `text` (default), `html` or `markdown`. |
| `includeLists` | Requirement/responsibility sections as arrays (default on). |
| `discoverFilters` | Output one filter-overview record per site instead of postings. |
| `onlyNewJobs`, `stateKey` | Daily feed mode, see below. |
| `maxItems` | Maximum records in total (default 100). |

### How Lever's own filters behave

These were checked against the live API:

- `teams`, `departments`, `commitments` and `levels` must be the **whole value**, but upper and lower case do not matter (`platform` finds `Platform`). A fragment such as `Plat` finds nothing.
- `locations` must be the whole value **with the exact capitalisation** (`London`, not `london`). A posting matches if **any** of its locations equals the value, not just the primary one.
- Several values in one field are OR (`teams: ["Platform", "Experience"]` returns both teams). Different fields are AND (team **and** location).
- Unknown values return no postings rather than an error. When that happens the run SUMMARY says so and suggests discovery.

`keyword`, `workplaceTypes`, `postedSince` and `salaryOnly` are applied by the Actor after download, because Lever has no parameter for them.

### Example input

```json
{
  "sites": ["spotify", "zoox", "https://jobs.eu.lever.co/pnlfin"],
  "teams": ["Platform", "Experience"],
  "workplaceTypes": ["remote", "hybrid"],
  "postedSince": "30 days",
  "descriptionFormat": "markdown",
  "maxItems": 50
}
```

Not sure which team names a company uses? Run once with `"discoverFilters": true`:

```json
{ "sites": ["spotify"], "discoverFilters": true }
```

and you get a record like this (shortened):

```json
{
  "recordType": "filters",
  "site": "spotify",
  "region": "global",
  "totalPostings": 82,
  "teams": [{ "value": "Platform", "count": 15 }, { "value": "Experience", "count": 11 }],
  "locations": [{ "value": "London", "count": 35 }, { "value": "Stockholm", "count": 31 }],
  "commitments": [{ "value": "Permanent", "count": 76 }, { "value": "Short Term", "count": 2 }],
  "workplaceTypes": [{ "value": "hybrid", "count": 41 }, { "value": "remote", "count": 27 }, { "value": "on-site", "count": 14 }]
}
```

Discovery applies your server-side filters (so you can drill down, for example team first, then see its locations) and ignores `keyword`, `workplaceTypes`, `postedSince`, `salaryOnly`, `onlyNewJobs` and `maxItems`. It returns one record per site.

## Output

One dataset record per posting. All field names are camelCase and stay stable.

```json
{
  "postingId": "29ab6640-f7dc-4c10-a4e9-0a12d1e3bcd8",
  "title": "Senior Commercial Product Manager",
  "site": "zoox",
  "region": "global",
  "team": "Finance",
  "department": "Finance, Places and Build",
  "location": "Foster City, CA",
  "allLocations": ["Foster City, CA"],
  "country": "US",
  "commitment": "Full-time",
  "level": null,
  "workplaceType": "hybrid",
  "remote": false,
  "salary": { "min": 165000, "max": 221000, "currency": "USD", "interval": "year" },
  "salaryText": "USD 165,000–221,000 per year",
  "salaryDescription": "Base Salary Range … (the employer's own pay note)",
  "hostedUrl": "https://jobs.lever.co/zoox/29ab6640-f7dc-4c10-a4e9-0a12d1e3bcd8",
  "applyUrl": "https://jobs.lever.co/zoox/29ab6640-f7dc-4c10-a4e9-0a12d1e3bcd8/apply",
  "createdAt": "2026-10-19T17:59:21.675Z",
  "opening": null,
  "description": "As the first product lead for commercial systems … (in the chosen format)",
  "additional": null,
  "lists": [
    { "heading": "In this role you will:", "items": ["Set the objectives that pricing, promotions, and fleet deployment …", "Partner day to day with Data Science on …"] },
    { "heading": "Qualifications", "items": ["8+ years in product management …", "…"] }
  ],
  "scrapedAt": "2026-10-19T20:01:37Z"
}
```

| Field | Notes |
|---|---|
| `postingId`, `site`, `region` | Identity. `postingId` is unique within a site; `region` is `global` or `eu`. |
| `title`, `team`, `department`, `location`, `allLocations`, `country`, `commitment`, `level` | Lever's categories. `null` when the company leaves them empty (`level` is used by few companies). `allLocations` lists every location, primary first, each once. |
| `workplaceType`, `remote` | `remote`, `hybrid`, `on-site` or `unspecified`. `remote` is true only for `remote`. |
| `salary`, `salaryText` | `null` unless the employer published a range. `interval` is usually `year`, `month`, `week`, `day` or `hour`; other Lever intervals pass through lower-cased (for example `biweekly`, `one-time`), and it is `null` when Lever gives none. |
| `salaryDescription` | The employer's own pay note, in the chosen format. Independent of `salary`: it can be present without a range, and is `null` for `descriptionFormat: none`. |
| `hostedUrl`, `applyUrl` | Public posting page and direct application page. |
| `createdAt` | When the posting was created, ISO 8601 UTC. |
| `opening`, `description`, `additional` | The three text sections, in the chosen format (`null` for `none` or an empty section). `description` is the main body without the opening, so a posting that has only an opening has `description: null`. |
| `lists` | `[{heading, items[]}]` with plain-text items; `null` when `includeLists` is off. |
| `scrapedAt` | When this run read the posting. |

The dataset has views for overview, salary, details and filter discovery.

### Run SUMMARY

Every run writes a `SUMMARY` record to the key-value store: for each site its `count`, a `reason` (`completed`, `no_matches`, `cap_reached`, `budget` or `error`), the region used and the number of postings on the board (after the server-side filters, which Lever applies before sending); the server-side and client-side filters that were applied; every error (invalid entries, unknown sites, failed requests); `unbilledJobs` (see Limits) and, in daily feed mode, the incremental statistics and `stateEvicted`.

## Daily feed: only new postings

Switch on `onlyNewJobs`, pick a `stateKey` (for example `weekly-eng`) and schedule the Actor. The first run delivers everything that matches. Later runs deliver only postings that earlier runs did not deliver.

- Postings are remembered per site and posting id in a **named key-value store** of your account, `lever-job-feed-state`. The identity is `site|postingId`; it does not depend on the region, the way the site was written (name, EU URL, posting URL) or any output option, so changing `descriptionFormat` or `includeLists` never makes old postings look new.
- A posting is remembered only after it was delivered **and** its charge was confirmed. If `maxItems` stops a run early, or the max charge is reached, the rest is delivered by the next run. A run that is restarted after a platform migration also saves the postings it had already delivered, even when nothing is left for it to do.
- A posting that is still on the board is never forgotten; one that has been off the board for 90 days is removed from the memory.
- Use a different `stateKey` for each schedule or filter setup. Two runs that share a key share their memory.
- The memory of a state key is stored in the record `STATE-<readable part of the key>-<12 hex characters>`; the hex part is a hash of the exact key you typed, so keys that only differ in punctuation, spaces or letters outside `a-z0-9` (`daily/a`, `daily?a`, `daily_a`, `daily` and ` daily `) never share a memory. An empty key means `default`.
- **Size cap.** One state key remembers at most 200,000 postings. Above that, the postings seen longest ago are removed first (`SUMMARY.stateEvicted` counts them). A removed posting that is still on a board is delivered, and charged, again. Real Lever boards are far smaller, so this only matters for very large site lists on one key; split them over several state keys.
- **Overlapping runs.** When a run starts with `onlyNewJobs`, it writes a small record `LOCK-STATE-…` in the same store with its run id and the time. If another run holds that lock and Apify reports it as running or ready, the new run stops at once with an error instead of delivering the same postings twice; a lock of a finished or unknown run is taken over, and the lock is removed at the end of the run. This is best effort: two runs that start within the same moment can both pass, and the check is skipped when the run id is not available (local runs). Schedule runs of one state key so they do not overlap.
- After a platform restart, a posting whose charge cannot be confirmed (see Limits) is not written to the memory, so the next run delivers it again and bills it properly.

## Pricing

Pay per event: a small start fee per run and a fixed price per record.

| Event | Price |
|---|---|
| Actor start (charged by Apify) | $0.00005 per GB of memory, at least one event per run |
| Job result (one posting, or one filter record per site) | $0.0015 |

The start event is counted once per GB of memory, and at least once. This Actor runs with 512 MB by default and accepts at most 1024 MB, so a run is always charged **exactly one** start event. The examples below are at the default memory:

| Postings | Calculation | Total |
|---|---|---|
| 20 | $0.00005 + 20 × $0.0015 | $0.03005 |
| 100 | $0.00005 + 100 × $0.0015 | $0.15005 |
| 1,000 | $0.00005 + 1,000 × $0.0015 | $1.50005 |

You pay only for records that are delivered. Sites that fail, postings that are filtered out and postings skipped as already delivered cost nothing. The Actor never delivers more records than your maximum charge per run allows: before it reads anything it checks how many Job results still fit, and each record is pushed together with its charge.

**`discoverFilters`:** it is billed with the same Job result event, **one per site record** (not per posting on the board, even though the whole board is read to build the record). Records count against the maximum charge like any other: if the limit only covers two of five sites, two records are delivered and the run stops. A site that fails produces no record and costs nothing. `maxItems` does not apply to discovery.

## Use with AI agents (MCP)

AI agents can call this Actor through Apify's MCP server at `https://mcp.apify.com`. This Actor is not an MCP server itself; it is a normal Apify Actor that the agent starts through Apify's server. To give an MCP-capable client (such as Claude Desktop, Cursor or VS Code) this Actor as a tool, add this server configuration:

```json
{
  "mcpServers": {
    "apify": {
      "type": "http",
      "url": "https://mcp.apify.com/?tools=fetch-actor-details,zendren/lever-job-feed"
    }
  }
}
```

Sign in with your Apify account (OAuth) or send your Apify token as a bearer header (`Authorization: Bearer <APIFY_TOKEN>`). Keep the token out of the URL.

**Agents: state the price before the first paid run.** A run costs $0.0015 per job result plus $0.00005 to start, so 20 postings cost $0.03005. Tell the user this, and use `maxItems` to keep the first run small.

1. The agent can read the Actor's details with `fetch-actor-details`, then calls the Actor tool `zendren/lever-job-feed` with the input (for example "list remote data engineering postings at spotify and zoox that publish a salary", or `discoverFilters` for "which teams and locations does pnlfin hire in?").
2. The tool starts a run and returns the run's information, including the id of its dataset.
3. The agent reads the records with the server's dataset tool, `get-dataset-items`, using that dataset id.

`maxItems` is part of the Actor input, so the agent (or you) can keep the result small. The maximum total charge is a run option, not an input field: set it where the run is started (the Console, the API or your MCP client's Apify settings). The Actor stops delivering when that limit is reached.

## API example

Run synchronously and get the dataset items back:

```bash
curl -X POST "https://api.apify.com/v2/acts/zendren~lever-job-feed/run-sync-get-dataset-items?maxTotalChargeUsd=0.05" \
  -H "Authorization: Bearer $APIFY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "sites": ["zoox"],
        "salaryOnly": true,
        "workplaceTypes": ["hybrid"],
        "descriptionFormat": "none",
        "maxItems": 20
      }'
```

`maxTotalChargeUsd` caps what the run can cost; the token travels in the `Authorization` header, never in the URL.

With the Apify client for Python:

```python
from apify_client import ApifyClient

client = ApifyClient("<APIFY_TOKEN>")
run = client.actor("zendren/lever-job-feed").call(run_input={
    "sites": ["spotify", "https://jobs.eu.lever.co/pnlfin"],
    "keyword": "engineer",
    "maxItems": 50,
})
for posting in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(posting["site"], posting["title"], posting["salaryText"])
```

## Scheduling

Open the Actor, choose **Schedules** and create a schedule (for example every morning). Turn on `onlyNewJobs` with a fixed `stateKey` and each run returns only the postings that appeared since the previous one. Combine it with an Apify integration (webhook, Google Sheets, Slack and others) to route the new postings where you need them.

## FAQ

**Which companies can I read?** Any company whose job board lives at `jobs.lever.co/<site>` or `jobs.eu.lever.co/<site>` and is public. Companies that moved to another applicant tracking system no longer have a Lever board.

**Do I need an account or an API key for Lever?** No. The Actor uses Lever's public Postings API, which needs no login.

**How do I get the exact team or location names?** Run with `discoverFilters: true`. The record lists every value with its posting count; copy values from there into `teams`, `departments`, `locations` or `commitments`.

**Why did my location filter return nothing?** Lever compares locations exactly, including capital letters, and against all of a posting's locations. `London` works, `london` and `Lon` do not. Teams, departments, commitments and levels ignore capitalisation but still need the whole value.

**What is the difference between global and EU?** Lever runs two separate instances. Companies hosted on the EU instance are not visible on the global one. With `region: auto` the Actor uses the host of a pasted URL, or tries the global instance first and the EU instance second for a plain name.

**Why is `salary` empty?** Only some companies publish salary ranges in Lever, often for specific countries or states. Use `salaryOnly: true` to keep just the postings that do.

**What does `workplaceType: unspecified` mean?** The company did not set remote, hybrid or on-site for that posting. Lever reports on-site as `onsite`; this Actor normalises it to `on-site`.

**Can I get only new postings every day?** Yes: schedule the Actor with `onlyNewJobs` and a `stateKey`. See "Daily feed" above.

**How are `maxItems` shared between sites?** Evenly. A site with fewer postings than its share hands the surplus to the others. Within a site, the newest postings come first.

**Are the descriptions the full text?** Yes, as Lever publishes them, split into `opening`, `description`, `additional` and the `lists` sections. Use `descriptionFormat: none` to leave the text out and keep the output small.

**What happens when a site fails?** The error is logged and written to the SUMMARY, and the other sites continue. You are not charged for the failed site.

## Related Actors

- `zendren/ats-job-feed`: Greenhouse, Lever, Ashby and Workday in one run, with a shared, simpler output.
- `zendren/greenhouse-job-feed`: Greenhouse only, with pay ranges and the department and office hierarchy.

## Limits

- Only public Lever job boards. Postings that are not public are not available.
- The data is exactly what the company published in Lever; fields such as level, salary or commitment are often empty.
- `keyword`, `workplaceTypes`, `postedSince` and `salaryOnly` are applied after download, so they reduce what you pay for but not what is read from Lever.
- Lever's API returns whole postings; very large boards (hundreds of postings) take a few seconds per site.
- The incremental memory is kept per `stateKey` in your account's named store; it is not shared between accounts. It holds at most 200,000 postings per key (see "Daily feed"), and its overlap lock is best effort.
- **Possible under-billing after a crash.** The Actor writes a record and charges it with Apify's combined push-and-charge call, which is not one atomic step. If a run is interrupted in the instant between the write and the charge, that single record is delivered but may not have been billed. The Actor never sends a charge again when it cannot tell whether the first one went through, so in that rare case you may get one record unbilled. Such records are listed in `SUMMARY.unbilledJobs`.
- This Actor is an independent tool and is not affiliated with or endorsed by Lever.
