Get any company's public **Greenhouse** job board as clean, structured JSON: open jobs with **pay ranges in real currency units**, the **department and office hierarchy**, custom fields, application form questions and a **new-jobs-only mode** for daily feeds. You pay **$0.0015 per job** delivered (plus a $0.00005 start fee), no subscription.

For a cheap first look, run one board with `maxItems` set to 20: 20 jobs cost **$0.03005** at the default 1,024 MB of memory.

It reads Greenhouse's public Job Board API (JSON, no login, no HTML scraping, no proxies). It is built for Greenhouse only, so the output is Greenhouse-shaped rather than lowest-common-denominator.

> Not affiliated with or endorsed by Greenhouse Software. The Actor only reads data that employers publish on their public job boards.

## What you get that a basic job list does not have

| Feature | What it does |
|---|---|
| **Pay ranges** | `payRanges` with `min`, `max`, `currency`, `title` and the disclosure text, converted from cents to units (`15000000` cents becomes `150000`). Many US employers publish several zones per job. |
| **Department hierarchy** | `departmentPath` such as `["EPD", "CTO Team", "CorpEng (Sub Team)"]`. Filter on a parent department and its child departments are included. |
| **Office hierarchy** | `officePath` such as `["North America", "USA", "San Francisco"]`. Filter on `North America` and every office below it is included. |
| **Custom fields** | The board's own metadata as `{name: value}` (employment type, level, workplace type, skills, ...). Types are kept: strings, lists, booleans. |
| **Application questions** | Label, required flag, field type (`input_text`, `textarea`, `input_file`, `multi_value_single_select`, ...) and answer options for each field of the apply form, with the location question, compliance groups (such as EEOC) and the demographic survey as separate fields. Useful for apply automation and research. |
| **Daily feed** | `onlyNewJobs` remembers what earlier runs delivered, so a scheduled run delivers (and bills) only jobs that appeared since. |
| **Content formats** | Description as plain text, HTML or Markdown (Greenhouse sends it HTML-escaped; the Actor decodes it). |
| **Stable IDs** | `jobId`, `internalJobId`, `requisitionId`, `boardToken`, plus `updatedAt` and `firstPublishedAt`. |

## How to find a company's board token

The token is the last part of the company's Greenhouse board address:

- `https://boards.greenhouse.io/`**`figma`** → token `figma`
- `https://job-boards.greenhouse.io/`**`dropbox`** → token `dropbox`
- Careers pages that embed Greenhouse often load `https://boards.greenhouse.io/embed/job_board?for=`**`figma`** in an iframe or script tag. View the page source and search for `greenhouse`.

You can paste the token or any of these URLs; the Actor extracts the token itself:

- a bare token: `figma`
- `boards.greenhouse.io/<token>` and `job-boards.greenhouse.io/<token>`, also a single job link `.../<token>/jobs/<id>` (the whole board is read)
- embed links `.../embed/job_board?for=<token>` and `.../embed/job_app?for=<token>&token=<id>`
- the API address `boards-api.greenhouse.io/v1/boards/<token>/jobs`

A link on the company's own domain that only carries `?gh_jid=123` does not contain the board token, so it cannot be resolved; the run SUMMARY says so and the other entries continue. EU-hosted Greenhouse addresses (`*.eu.greenhouse.io`) are not supported and are reported the same way.

## Input

| Field | Type | Default | What it does |
|---|---|---|---|
| `boards` | list, required | – (form prefill: `figma`, `dropbox`) | Board tokens or Greenhouse URLs. A bad entry is reported and the rest still run. |
| `departments` | list | – | Department names to keep. A parent includes its children. |
| `offices` | list | – | Office names to keep. A parent includes its children. |
| `keyword` | text | – | Match on title and department names. Spaces = all words; commas = alternatives. |
| `location` | text | – | Substring of the job location or an office name. Commas = alternatives. |
| `remoteOnly` | boolean | `false` | Heuristic, see below. |
| `updatedSince` | text | – | A date (`2026-09-01`) or relative (`7 days`, `2 weeks`, `12 hours`, `1 month`). |
| `updatedSinceField` | `updatedAt` / `firstPublishedAt` | `updatedAt` | Which date `updatedSince` compares. |
| `includeContent` | `none` / `text` / `html` / `markdown` | `text` | Format of `content`. |
| `includePayRanges` | boolean | `false` | Add `payRanges` (one extra request per delivered job). |
| `includeQuestions` | boolean | `false` | Add `questions`, `locationQuestions`, `complianceQuestions` and `demographicQuestions` (one extra request per delivered job, shared with pay ranges). |
| `includeCustomFields` | boolean | `true` | Add `customFields`. |
| `onlyNewJobs` | boolean | `false` | Incremental mode, see below. |
| `stateKey` | text | `default` | Name of the incremental history. Runs with the same key must not overlap. |
| `maxItems` | integer | `100` | Total jobs to deliver, split evenly across boards. |

All filters combine with AND. **Department and office names** are matched exactly (case-insensitive). If a name does not exist on a board the run log and `SUMMARY.unmatchedFilters` say so, and the `DEPARTMENTS_OFFICES` record lists the real names with open-job counts.

**Remote heuristic.** `remote` is `true` when the location or an office name contains *remote*, *work from home*, *WFH* or *anywhere*, when the title has a stand-alone *remote* (not "remote sensing" or "remote access"), or when a custom field named like *workplace type* says Remote. "Hybrid" and "on-site" never set it. Greenhouse has no official remote flag, so treat it as a good first filter, not a guarantee.

**Order and `maxItems`.** Jobs are taken most recently updated first. When a run is capped, each board gets an equal share and a board with fewer jobs hands its unused share to the others.

### Example input

```json
{
  "boards": ["figma", "https://job-boards.greenhouse.io/dropbox"],
  "departments": ["Engineering"],
  "offices": ["North America"],
  "updatedSince": "14 days",
  "includeContent": "markdown",
  "includePayRanges": true,
  "maxItems": 200
}
```

## Output

Each dataset item is one job. Field names are stable camelCase. This is a real record from a live run on 2026-10-19 (board `figma`, `includePayRanges` on), shortened: `content` and the pay disclosure text are cut.

```json
{
  "jobId": 6223657004,
  "internalJobId": 5228605004,
  "requisitionId": "2649",
  "title": "Manager, Software Engineering - Production Engineering",
  "boardToken": "figma",
  "companyName": "Figma",
  "location": "San Francisco, CA • New York, NY • United States",
  "offices": ["Canada", "US"],
  "officePath": ["North America", "Canada"],
  "departments": ["Engineering"],
  "departmentPath": ["Engineering"],
  "remote": false,
  "absoluteUrl": "https://boards.greenhouse.io/figma/jobs/6223657004?gh_jid=6223657004",
  "applyUrl": "https://job-boards.greenhouse.io/figma/jobs/6223657004",
  "updatedAt": "2026-10-19T13:49:15-04:00",
  "firstPublishedAt": "2026-10-19T13:49:15-04:00",
  "language": "en",
  "content": "Figma is growing our team of passionate creatives and builders on a mission to make design accessible to all. ...",
  "customFields": {},
  "payRanges": [
    {
      "min": 258000,
      "max": 376000,
      "currency": "USD",
      "title": "Annual Base Salary Range:",
      "description": "Pay Transparency Disclosure\n\nJob level and actual compensation will be decided based on factors including ..."
    }
  ],
  "scrapedAt": "2026-10-19T20:31:38+00:00"
}
```

Note how `location` (the text of the post) and `officePath` can differ: `officePath` follows the first office Greenhouse lists for the job (`Canada`), while the post text names San Francisco and New York. Boards are free to configure these independently.

| Field | Notes |
|---|---|
| `jobId`, `internalJobId`, `requisitionId` | Post id, Greenhouse's internal job id, and the employer's requisition id (may be `null`). |
| `title`, `companyName`, `boardToken` | `boardToken` + `jobId` identify a job. |
| `location`, `offices`, `officePath` | Location text of the post, the names of **all** offices of the job (`offices`), and the hierarchy path of the **first** office only (`officePath`). The order is the one Greenhouse returns, so for a job with several offices read `offices` for the full list. |
| `departments`, `departmentPath` | Names of all departments of the job, and the hierarchy path of the first department only. |
| `remote` | Heuristic, see above. |
| `absoluteUrl` | Public job URL from Greenhouse (often the company's own careers page). |
| `applyUrl` | Greenhouse-hosted job URL `job-boards.greenhouse.io/<token>/jobs/<id>`; redirects to the company page if the board is embedded there. |
| `updatedAt`, `firstPublishedAt` | ISO 8601 with offset. `firstPublishedAt` is `null` when the board does not report it. |
| `language` | Post language code. |
| `content` | Only if `includeContent` is not `none`. |
| `customFields` | Only if `includeCustomFields` is on. `{}` when the board has none. |
| `payRanges` | Only with `includePayRanges`. `[]` = the job publishes no pay range; `null` = the detail request failed (listed in `SUMMARY.detailFailures`); with `onlyNewJobs` such a job is held back instead and retried on a later run. A job whose detail page is gone (removed while the run was reading) is not delivered. Amounts are in currency units; `title` and `description` carry the context (annual, hourly, zone, location). |
| `questions` | Only with `includeQuestions`: `{label, required, type, name, description, options: [{label, value}]}`, one entry per form field. A "Resume/CV" question with upload and paste fields gives two entries. `null` = the detail request failed. |
| `locationQuestions` | Only with `includeQuestions`: the form's location question(s), same shape as `questions`. The hidden latitude and longitude helper fields are left out. |
| `complianceQuestions` | Only with `includeQuestions`: compliance groups such as EEOC, `[{type, description, questions: [...]}]` with questions shaped like `questions`. `[]` = none. |
| `demographicQuestions` | Only with `includeQuestions`: the demographic survey `{header, description, questions: [{id, label, required, type, options: [{label, value, freeForm}]}]}`, or `null` when the board has none. |
| `scrapedAt` | UTC time the record was produced. |

Pay ranges depend on the employer: boards in jurisdictions with pay-transparency laws usually publish them, others do not.

### SUMMARY record

Every run writes a `SUMMARY` record to the default key-value store:

```json
{
  "jobsPushed": 10,
  "stoppedReason": "maxItems",
  "boards": {
    "figma":   { "name": "Figma",   "count": 5, "reason": "cap_reached", "jobsOnBoard": 140, "matchedFilters": 140 },
    "dropbox": { "name": "Dropbox", "count": 5, "reason": "cap_reached", "jobsOnBoard": 18,  "matchedFilters": 18 }
  },
  "errors": {},
  "invalidInputs": [],
  "incremental": { "firstRun": true, "newJobsDelivered": 10, "alreadySeenSkipped": 0, "trackedJobs": 10, "pruned": 0, "stateEvicted": 0 }
}
```

Per-board `reason` is one of `completed`, `no_matches` (the board was read and nothing new passed the filters), `cap_reached` (`maxItems`), `error` (see `errors`) or `budget` (your maximum total charge was reached). `unmatchedFilters`, `hierarchyUnavailable` and `detailFailures` explain partial results.

A second record, `DEPARTMENTS_OFFICES`, holds the department and office trees of each board read (`id`, `name`, `children`, and `jobs`: the number of distinct open jobs in that node and everything below it, so a job attached to a parent and to its child counts once). Run once with `maxItems` equal to the number of boards (one job per board) to see the exact names to put into `departments` and `offices`; with a smaller `maxItems` the run can end before every board was read.

## Daily feed: only new jobs

Set `onlyNewJobs` to `true` and give the schedule its own `stateKey`.

- The first run delivers everything that matches and remembers it.
- Later runs deliver only jobs not delivered before. Jobs you did not receive because of `maxItems` are still new and come in the next run.
- A job is remembered only after it was delivered and billed, so a failed or budget-stopped run never loses jobs.
- History is kept in the named key-value store `greenhouse-job-feed-state` in your Apify account, one record per `stateKey`. A job that stays on its board keeps its entry fresh; entries not seen for 90 days are dropped, so a job that disappears for 90 days and comes back is delivered again.
- A job is remembered only if its charge was confirmed. After a platform restart, a row that reached the dataset but whose charge could not be confirmed is kept out of the history (see `SUMMARY.unbilledJobs`), so the next incremental run delivers and bills it again.
- The history holds at most 200,000 jobs. Beyond that the least recently seen entries are dropped first (`SUMMARY.incremental.stateEvicted` counts them); a dropped job that is still on its board is delivered, and billed, again.
- Use different state keys for different board sets or schedules; delete the record to start over. Each key has its own record, so keys such as `daily/a` and `daily_a` never share history.
- Runs with the same `stateKey` must not overlap. A run that starts while another run using the same key is still running stops with an error and delivers nothing. This check is best effort (it is not atomic, so two runs started in the very same moment can both pass); schedule runs with one key one after the other.

## Pricing

Pay per event, no subscription: you pay for jobs delivered, not for compute time.

| Event | Price |
|---|---|
| Actor start (charged by Apify, once per GB of run memory, rounded up, minimum one) | $0.00005 |
| Job result (one job in the dataset) | $0.0015 |

The memory of this Actor is capped at 1,024 MB, so there is always exactly one start event per run. The totals below are at the default 1,024 MB.

| Jobs delivered (at 1,024 MB) | Calculation | Total |
|---|---|---|
| 20 | $0.00005 + 20 × $0.0015 | **$0.03005** |
| 100 | $0.00005 + 100 × $0.0015 | **$0.15005** |
| 1,000 | $0.00005 + 1,000 × $0.0015 | **$1.50005** |

Pay ranges, application questions, custom fields and every content format cost the same per job. The Actor never delivers more jobs than your **maximum total charge per run** allows. With `onlyNewJobs`, a repeat run on an unchanged board delivers 0 jobs and costs only the single $0.00005 start fee.

## Speed

The job list of a board arrives in one request (about 3 seconds for a 700-job board). Pay ranges and questions need one extra request per delivered job; the Actor keeps the request rate polite and reaches about 6 jobs per second with them on (140 jobs in roughly 20 seconds). Leave them off, or use filters and `maxItems`, when you only need the list.

## Use with AI agents (MCP)

AI agents and MCP-compatible clients can call this Actor through Apify's MCP server at [mcp.apify.com](https://mcp.apify.com). Example client configuration that exposes this Actor and the Actor-details tool:

```json
{
  "mcpServers": {
    "apify": {
      "type": "http",
      "url": "https://mcp.apify.com/?tools=fetch-actor-details,zendren/greenhouse-job-feed"
    }
  }
}
```

Authenticate with your Apify account: OAuth in the client, or your API token as an `Authorization: Bearer <YOUR_API_TOKEN>` header. The agent then works in two steps:

1. It starts a run through the Actor's tool with the same JSON input as above (for example `boards`, `departments`, `includePayRanges`, `maxItems`). The tool call returns the run status and the storage IDs (run ID, default dataset ID), not the job rows. If the run is still going, the agent can poll the run status.
2. It reads the jobs with the server's dataset tool, `get-dataset-items`, using the dataset ID from step 1. The server adds this and the other result-reading tools next to the Actor tool.

Instructions for agents:

- **State the price before the first paid run**: $0.0015 per job delivered plus a $0.00005 start fee, so 20 jobs cost $0.03005.
- Use a small `maxItems` (20 is enough to check a board) so a run stays cheap and short. `maxItems` is an input of this Actor; your maximum total charge is a run option set by the client or the agent, not an Actor input.

## Use from code

Run it and get the items in one call. The token goes in the `Authorization` header, never in the URL (replace `<YOUR_API_TOKEN>` with your Apify API token). `maxTotalChargeUsd` caps what the run can charge:

```bash
curl -X POST \
  "https://api.apify.com/v2/acts/zendren~greenhouse-job-feed/run-sync-get-dataset-items?maxTotalChargeUsd=0.05" \
  -H "Authorization: Bearer <YOUR_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"boards": ["figma"], "departments": ["Engineering"], "includePayRanges": true, "maxItems": 20}'
```

Python:

```python
from apify_client import ApifyClient

client = ApifyClient("<YOUR_API_TOKEN>")
run = client.actor("zendren/greenhouse-job-feed").call(
    run_input={
        "boards": ["figma", "dropbox"],
        "updatedSince": "7 days",
        "includePayRanges": True,
        "maxItems": 100,
    },
    max_total_charge_usd=0.2,
)
for job in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(job["companyName"], job["title"], job["payRanges"])
```

For long runs (pay ranges on big boards) use the asynchronous run endpoint instead of `run-sync`.

## Scheduling and integrations

In Apify Console open **Schedules**, create a schedule (for example daily at 07:00), add this Actor as the task, and use an input with `onlyNewJobs: true`, a `stateKey` of your choice and an `updatedSince` or `maxItems` that fits your volume.

Results are available through the Apify API and can be sent on with Apify's integrations: Make, n8n and Zapier, Google Sheets, Slack, or a webhook that fires when a run finishes. You can also fetch the dataset from your own pipeline.

## Related Actors

- [zendren/ats-job-feed](https://apify.com/zendren/ats-job-feed): Greenhouse, Lever, Ashby and Workday boards in one run.
- [zendren/lever-job-feed](https://apify.com/zendren/lever-job-feed): Lever only, with server-side filters, salary and EU sites.

## FAQ

**Where does the data come from?**
From the public Job Board API that Greenhouse offers for every job board. It is the same data the board page shows. The Actor does not log in, solve CAPTCHAs, or parse HTML pages.

**Does it work for every company that uses Greenhouse?**
For every board that is publicly reachable at `boards.greenhouse.io` / `job-boards.greenhouse.io` (this includes boards embedded on company websites). Boards that are private, disabled, or hosted only on Greenhouse's EU address are not covered. An unknown token is reported as `board not found` in the SUMMARY.

**Why does a job have an empty `payRanges`?**
The employer did not publish a pay range for that job. Pay transparency data is optional in Greenhouse. `payRanges` is `null` only if the extra request for that job failed.

**Are the amounts yearly or hourly?**
Greenhouse does not send an interval field. Read `title` and `description`, which usually say "Annual Base Salary Range", "Hourly Rate" or the zone. Amounts are in the stated `currency`, not in cents.

**How do I get only engineering jobs in a region?**
Set `departments` to `Engineering` and `offices` to the region's office name (for example `North America`). Child departments and child offices are included automatically. Check the `DEPARTMENTS_OFFICES` record for the exact names on a board.

**Why did my department filter return nothing?**
Names must match exactly (case-insensitive). The run log and `SUMMARY.unmatchedFilters` tell you which names do not exist on which board.

**What is the difference between `updatedAt` and `firstPublishedAt`?**
`updatedAt` changes whenever the post is edited; `firstPublishedAt` is when it first went live (not every board reports it). `updatedSinceField` decides which one `updatedSince` uses.

**How does incremental mode know a job is new?**
By board token and job id. A job already delivered under the same `stateKey` is skipped and not billed. See "Daily feed" above.

**Will I be billed for jobs I did not receive?**
No. A job is billed when it is added to the dataset, and the Actor stops before your maximum total charge would be exceeded. After a platform restart, jobs that were already delivered are not delivered or billed twice.

**Can I read several companies in one run?**
Yes, `boards` takes any number of entries. `maxItems` is shared fairly between them. For Lever, Ashby or Workday boards see "Related Actors" above.

**Can I get the application form?**
Yes, set `includeQuestions`. Each form field comes with its label, whether it is required, its type and its options. Location, compliance (such as EEOC) and demographic questions arrive in their own fields. The Actor only reads the form definition; it does not submit applications.

## Limits

- Public boards only; no authentication, no EU-hosted boards, no private or draft jobs.
- **Billing is not atomic with delivery.** Each job is written to the dataset and then charged, using Apify's charge-aware push. If a run is killed in the tiny gap between the two, that one job is already in your dataset and its charge may or may not have gone through. The Actor never sends an uncertain charge a second time, so the worst case is that such a job is not billed. It is listed in `SUMMARY.unbilledJobs`, counts toward `maxItems`, and is not written into the incremental history (an `onlyNewJobs` run delivers and bills it again). You are never billed twice, and never beyond your maximum total charge.
- Runs that use the same `stateKey` must not overlap (see "Daily feed").
- Applicant counts, salary estimates, and anything Greenhouse does not publish are not available.
- Department and office hierarchy is the one the employer configured; a few boards have flat lists, so paths then contain a single name.
- `remote` is a text heuristic.
- Pay ranges and questions cost one request per delivered job, so big runs with them take minutes.
- Greenhouse may rate-limit; the Actor slows down and retries, and honours `Retry-After`. A board that keeps failing is reported in `SUMMARY.errors` and the other boards continue.
