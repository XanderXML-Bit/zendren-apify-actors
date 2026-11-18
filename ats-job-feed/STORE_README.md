Scrape the open jobs of companies on **Greenhouse, Lever, Ashby and Workday** into one clean, normalized feed you can export as **JSON, CSV or Excel** or read through the Apify API. One jobs scraper and one output schema for all four applicant tracking systems (ATS). You pay **$0.0015 per job** delivered, plus $0.00005 per run to start, so a cheap first look at 20 jobs costs **$0.03005**.

It reads the public job data that employers already publish for their own career pages, live when the run starts: no browser, proxy or login. Turn on **New jobs only** and a daily schedule delivers, and charges for, just the postings that appeared since the last run.

Built for job boards, recruiters, sales-signal teams and AI agents that need the same companies checked every day without maintaining four scrapers.

## What it does

* Takes career-site URLs or short `ats:slug` names for **Greenhouse**, **Lever**, **Ashby** and **Workday** and fetches every open job.
* Normalizes them into the same fields (title, company, location, remote flag, department, posted date, apply URL, plain-text description, salary when published).
* Filters by keyword, location and remote-only.
* Splits `maxItems` fairly across your companies, so one large employer cannot crowd out the others.
* Optionally remembers what it has already delivered (**New jobs only**), so repeated runs return only fresh jobs.

## Who it's for

* **Job boards and newsletters** that aggregate openings from specific employers.
* **Recruiters and sourcers** tracking hiring at target companies.
* **Sales and market-intelligence teams** that treat new roles as a buying or growth signal.
* **Researchers and analysts** studying hiring, locations, departments or published pay.
* **Developers and AI agents** who want job data as an API call instead of maintaining four integrations.

## How to use

1. Add companies under **Companies**. Paste a career-site URL or use the `ats:slug` shortcut:
   * `https://boards.greenhouse.io/stripe` or `greenhouse:stripe`
   * `https://jobs.lever.co/spotify` or `lever:spotify`
   * `https://jobs.ashbyhq.com/ramp` or `ashby:ramp`
   * `https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite` or `workday:nvidia.wd5/NVIDIAExternalCareerSite`
2. Optionally set **Keyword**, **Location**, **Remote only** and **Max items**. For a cheap first look, set **Max items** to 20 ($0.03005 at most).
3. Run it, then open the dataset and export as JSON, CSV, Excel and more, or fetch it with the API.

The prefilled form has four companies and `maxItems` 100, so it costs at most $0.15005.

**Cheap first look: 20 jobs from one board**

```json
{
  "companies": ["greenhouse:stripe"],
  "keyword": "engineer",
  "maxItems": 20
}
```

**One-off search across systems**

```json
{
  "companies": ["greenhouse:stripe", "lever:spotify", "ashby:ramp", "workday:nvidia.wd5/NVIDIAExternalCareerSite"],
  "keyword": "data engineer, analyst",
  "location": "Berlin, Remote",
  "maxItems": 200
}
```

**Daily schedule: only jobs posted since the last run**

```json
{
  "companies": ["greenhouse:stripe", "lever:spotify", "ashby:ramp"],
  "keyword": "engineer",
  "onlyNewJobs": true,
  "stateKey": "engineering-watch",
  "maxItems": 500
}
```

The first run returns every matching job (up to `maxItems`) and creates the memory. Every later run with the same `stateKey` returns only jobs it has not delivered before. See [Incremental mode](#incremental-mode-new-jobs-only).

## Input

| Field | Type | Description |
|-------|------|-------------|
| `companies` | list, required | Career-site URLs or `ats:slug` entries (see above). Comma-separated entries are accepted too. |
| `keyword` | string | Case-insensitive match on job title and department. Words separated by spaces must all match; separate alternatives with commas (`data engineer, analyst`). |
| `location` | string | Case-insensitive substring match on the job's locations. Comma = OR. The words of one entry must appear in the same location. |
| `remoteOnly` | boolean | Only jobs the employer flags as remote. Default `false`. |
| `onlyNewJobs` | boolean | Return only jobs not delivered by an earlier run with the same state key. Default `false`. |
| `stateKey` | string | Name of the memory used by `onlyNewJobs`. Empty = derived from your company list (slugs are case-sensitive: `greenhouse:Stripe` and `greenhouse:stripe` give different memories). Use a new name to start over. |
| `maxItems` | integer | Maximum jobs delivered (and paid for) per run. Default 100, up to 100,000. Split fairly across companies. With `onlyNewJobs` it caps the new jobs. |
| `includeDescription` | boolean | Include the plain-text description. Default `true`. Turning it off is faster for Workday, which needs one extra request per job. |

## Output

Each dataset item is one job. This is a real record from the prefilled Stripe board (Greenhouse), shortened (`descriptionText` is cut here) and showing the field shape. Postings change daily, so what you get depends on what the company lists when you run.

```json
{
  "title": "Abuse Research Engineer",
  "company": "Stripe",
  "location": "Remote from the US",
  "remote": true,
  "department": "8611 Security Analytics",
  "postedDate": "2026-09-09T10:52:09-04:00",
  "applyUrl": "https://stripe.com/jobs/search?gh_jid=8172503",
  "atsSource": "greenhouse",
  "descriptionText": "Who we are\n\nAbout Stripe\n\nStripe is a financial infrastructu …",
  "jobId": "8172503",
  "salary": null,
  "jobUrl": "https://stripe.com/jobs/search?gh_jid=8172503",
  "employmentType": null,
  "locations": ["Remote from the US", "US"],
  "companySlug": "stripe",
  "departments": ["8611 Security Analytics"]
}
```

When pay is published, `salary` looks like this (from an Ashby board):

```json
"salary": {"min": 211400, "max": 290600, "currency": "USD", "interval": "1 YEAR", "summary": "$211.4K - $290.6K"}
```

| Field | Meaning |
|-------|---------|
| `title`, `company` | Job title and employer. Greenhouse supplies the real company name; for the other systems it is derived from the board slug (`ramp` becomes `Ramp`). |
| `location`, `locations` | Primary location and every listed location. |
| `remote` | `true` if the employer flags the job as remote (or lists "Remote" as a location). |
| `department`, `departments` | First department and all departments. |
| `postedDate` | ISO 8601 date as reported by the ATS (Workday: date only, or `null` for "Posted 30+ days ago" when descriptions are off). |
| `applyUrl`, `jobUrl` | Where to apply and the public job page. |
| `atsSource`, `companySlug`, `jobId` | Which system the job came from, the board slug and the ATS job id. Together they identify a job. |
| `descriptionText` | Plain-text description, or `null` when descriptions are off. |
| `salary` | `{min, max, currency, interval, summary}` when the employer publishes pay, otherwise `null`. Often `null`; Workday has no structured pay. |
| `employmentType` | Full-time, contract and so on, when provided. |

A **`SUMMARY`** record in the run's key-value store lists every company you asked for with a count and a reason (`completed`, `no_matches`, `no_new_jobs`, `cap_reached`, `error`, `budget`), the failed companies and why the run stopped. In incremental mode it also reports `newJobs`, `skippedSeenJobs`, `stateKey`, `stateStore` and `stateEvicted`.

## Incremental mode (new jobs only)

Set `onlyNewJobs` to `true` and the Actor keeps a small memory of the jobs it has delivered, in a named key-value store called `ats-job-feed-state` in your own Apify account.

* **First run:** no memory exists yet, so every matching job is returned (up to `maxItems`) and the memory is created.
* **Later runs:** jobs already in the memory are skipped and not charged. Only new postings are delivered.
* **Filters first:** `keyword`, `location` and `remoteOnly` are applied before the memory check, so you can change filters freely. A job is only remembered once it has been delivered.
* **`maxItems` caps new jobs.** Jobs that did not fit under `maxItems` (or under your maximum charge) are not marked as seen and come out on the next run.
* **Which memory:** `stateKey` names it. Leave it empty and the key is derived from your company list, in any order (but case-sensitive: `greenhouse:Stripe` and `greenhouse:stripe` are different lists). Change the list and you get a fresh memory; set your own `stateKey` to keep one memory while you add or remove companies.
* **Reset:** use a new `stateKey`, or delete the `STATE-…` record from the `ats-job-feed-state` store.
* **Cleanup:** a job that has not been seen for 90 days is forgotten. The memory also holds at most 200,000 jobs per `stateKey`: beyond that the least recently seen ones are forgotten (reported as `stateEvicted` in `SUMMARY`) and are delivered again if they are still listed. For very large feeds, use several state keys.
* **Only paid jobs are remembered:** a job is added to the memory only once its charge is confirmed. If a run is interrupted at exactly the wrong moment, a job may be in the dataset without a confirmed charge; it is listed in `unbilledJobs` in `SUMMARY` and is not remembered, so a later run can deliver it again.
* **Restarts:** progress is saved during the run, so if the platform restarts it mid-run no job is delivered twice and none is lost.

It detects new postings (a new job id). An edit to an existing posting is not re-delivered, and removed postings are not reported. Avoid running two jobs with the same `stateKey` at the same time.

## Pricing

This is a pay-per-event Actor. You pay only for these events:

* **Run start:** $0.00005 per run, charged by Apify at the default 1,024 MB of memory.
* **Job result** (event `job-scraped`): $0.0015 per job delivered to the dataset.

There is no separate charge for compute or storage on top of these events. Examples:

| Jobs delivered | Arithmetic | Cost |
|---------------:|------------|-----:|
| 20 | $0.00005 + 20 × $0.0015 | $0.03005 |
| 100 | $0.00005 + 100 × $0.0015 | $0.15005 |
| 1,000 | $0.00005 + 1,000 × $0.0015 | $1.50005 |

You are never charged for a job that was not delivered. `maxItems` limits how many jobs a run delivers, and the **maximum cost per run** setting in Apify caps spend as well: the Actor stops before it would push a job that cannot be paid for. With **New jobs only**, a daily schedule pays only for the new postings each day; a day with nothing new costs just the run-start event.

## Use with AI agents (MCP) and the API

This Actor is a tool on [Apify's MCP server](https://mcp.apify.com), not itself an MCP server. Add it to the server's tool list in your MCP client configuration, then sign in with OAuth, or send your Apify API token as a bearer token in the `Authorization` header:

```json
{
  "mcpServers": {
    "apify": {
      "type": "http",
      "url": "https://mcp.apify.com/?tools=fetch-actor-details,zendren/ats-job-feed"
    }
  }
}
```

The exact config format differs between MCP clients; see the Apify MCP server documentation for yours.

Example prompt:

> Find remote data engineering jobs at Stripe, Spotify and Ramp. Give me up to 20 and include the apply links.

The input the agent would send to the Actor:

```json
{
  "companies": ["greenhouse:stripe", "lever:spotify", "ashby:ramp"],
  "keyword": "data engineer",
  "remoteOnly": true,
  "maxItems": 20,
  "includeDescription": false
}
```

**Agents: state the price before the first paid run.** This call costs $0.0015 per job delivered plus $0.00005 to start, at most $0.03005 for 20 jobs, and less if fewer jobs match. Keep `maxItems` small, and for repeated checks use `onlyNewJobs` with a fixed `stateKey`.

How the agent gets the results: the Actor tool starts a run and returns the run's status and storage IDs, not the job records themselves. The agent then waits for the run to finish (the server's `get-actor-run` tool, if needed) and reads the jobs with the server's `get-dataset-items` tool, using the dataset ID from the run (paging through larger results). Apify's MCP server adds these helper tools automatically when you select an Actor. `maxItems` is part of the Actor input; the maximum cost per run is a run option.

### API

Run it synchronously and get the jobs back in one request. Send the token in the `Authorization` header, never in the URL. `maxTotalChargeUsd` caps what the run can spend; the 20-job example costs at most $0.03005, so `0.04` leaves a little room:

```bash
curl -X POST "https://api.apify.com/v2/acts/zendren~ats-job-feed/run-sync-get-dataset-items?maxTotalChargeUsd=0.04" \
  -H "Authorization: Bearer $APIFY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"companies": ["greenhouse:stripe"], "keyword": "engineer", "maxItems": 20, "includeDescription": false}'
```

Add `&format=csv` to get CSV instead of JSON. Synchronous calls time out after 5 minutes, so use an asynchronous run and the dataset endpoint for big ones.

## Scheduling and integrations

* **Schedules:** in Apify Console, create a schedule (for example daily at 07:00) for this Actor with the input above and `onlyNewJobs: true`.
* **API:** start runs and read datasets from your own code (see above).
* **Webhooks:** add a webhook on `ACTOR.RUN.SUCCEEDED` to call your own endpoint when a run finishes, then read the dataset of that run.
* **Integrations:** send the dataset on with Apify's integrations, for example to Google Sheets, Make, n8n, Zapier or Slack.

## FAQ

**Is this legal, and what data does it read?**
It reads public job postings that employers publish for their own career sites, through the same public JSON endpoints those pages use. No login is used and no candidate or personal applicant data is touched. You are responsible for how you use the data and for following the terms of the employers and platforms involved. This is not legal advice.

**What do 20, 100 and 1,000 jobs cost?**
$0.03005, $0.15005 and $1.50005. Each is the $0.00005 run start plus $0.0015 per job delivered. If fewer jobs match, it costs less.

**How do I find out which ATS a company uses?**
Open the company's careers page and click a job. The address of the job or apply page tells you: `boards.greenhouse.io` or `job-boards.greenhouse.io` is Greenhouse, `jobs.lever.co` is Lever, `jobs.ashbyhq.com` is Ashby and `*.myworkdayjobs.com` is Workday. Paste that URL straight into **Companies**.

**Why does a company return 0 jobs?**
Check `SUMMARY.perCompany` for the reason. `no_matches` means the board was read but nothing passed your filters. `no_new_jobs` means every matching job was delivered by an earlier run. `error` means the board could not be read, usually a wrong slug, a board that is private or closed, or a company that moved to another ATS (the error text is in `failedCompanies`). `cap_reached` means `maxItems` ended the run before that company's turn.

**How does incremental mode work, and how do I reset it?**
With `onlyNewJobs` the Actor stores the ids of delivered jobs in the `ats-job-feed-state` named key-value store and skips them next time (details above). To start over, use a new `stateKey` or delete the `STATE-…` record from that store.

**How fresh is the data?**
Jobs are fetched live from the employer's career system on every run. Nothing is cached by this Actor, so a run shows what the employer's site shows at that moment.

**Does it need proxies or a login?**
No. It uses plain HTTPS requests to public endpoints, with per-host rate limiting and retries. You do not need to configure proxies or provide credentials.

**What is the Workday format?**
Use `workday:tenant.wdN/SiteName`, for example `workday:nvidia.wd5/NVIDIAExternalCareerSite`. Easiest is to paste the full careers URL, such as `https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite` (a language segment like `/en-US/` is fine). 

**What are the limits?**
`maxItems` can be up to 100,000 per run. Workday returns at most 3,000 postings per search; if a tenant has more, the run says so in the log and in `SUMMARY.truncatedCompanies`, and you can narrow it with `keyword`. Greenhouse, Lever and Ashby return a company's whole board, which is then filtered. The Actor runs with 1 GB of memory by default, which is enough for thousands of jobs with descriptions.

**Why is salary often null?**
`salary` is filled only when the employer publishes a pay range in its posting (Greenhouse, Lever and Ashby support it, many boards leave it empty). Workday has no structured pay, so Workday jobs always have `salary: null`.

**Where does the company name come from?**
Greenhouse supplies the real company name. For Lever, Ashby and Workday it is derived from the board slug: `ramp` becomes `Ramp`, `my-company` becomes `My Company`. Use `companySlug` for a stable identifier.

**Does `myworkdaysite.com` work?**
No. Workday career sites on `*.myworkdayjobs.com` are supported; sites hosted on `*.myworkdaysite.com` are not.

**Can I get only remote jobs?**
Yes, set `remoteOnly` to `true`. A job counts as remote when the employer flags it so (Lever and Ashby workplace type, Workday remote type) or lists "Remote" as a location (Greenhouse and Workday). You can also put `Remote` in `location`.

**Which export formats are available?**
The dataset can be downloaded as JSON, JSONL, CSV, Excel, XML, HTML table or RSS, or read through the API.

## Notes and limits

* Only public job boards are read. The Actor respects rate limits with per-host concurrency, back-off and `Retry-After`.
* Workday does not expose structured salary. "Posted 30+ days ago" jobs have no exact date unless descriptions are included.
* If a Workday detail request fails, the job is still returned (without description) and the failure is counted in `SUMMARY.detailFailures`. A Workday job keeps the same `jobId` with or without descriptions, so incremental mode does not deliver it twice.
* In incremental mode, a job whose charge could not be confirmed after a platform restart is not remembered and may be delivered (and charged) again by a later run; at most 200,000 jobs are remembered per `stateKey`.
* If a company's board is missing or private it is skipped and reported in `SUMMARY`; the rest of the run continues.
* If a run is restarted by the platform, it continues without delivering or charging a job twice.

## Related Actors

Greenhouse-only and Lever-only feeds with deeper platform fields are available as [`zendren/greenhouse-job-feed`](https://apify.com/zendren/greenhouse-job-feed) and [`zendren/lever-job-feed`](https://apify.com/zendren/lever-job-feed).

Found a company that does not work? Open an issue from the Actor page.
