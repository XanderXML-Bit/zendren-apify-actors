# apify-jobfeed

An Apify Actor (Python, `apify` SDK + `httpx`) that returns a normalized job feed straight from the
**public JSON endpoints of applicant-tracking systems** – no HTML scraping, no browser.

| ATS | Endpoint used |
|-----|---------------|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Lever | `GET api.lever.co/v0/postings/{slug}?mode=json` (falls back to `api.eu.lever.co` on 404) |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` |
| Workday | `POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (limit 20, paged) + per-job detail `GET …/wday/cxs/{tenant}/{site}{externalPath}` |

## Layout

```
src/main.py            Apify entrypoint (input, dataset push, charging, restart state)
src/incremental.py     "new jobs only" state: named-store record of delivered job ids (no Apify import)
src/pipeline.py        runs companies concurrently, round-robin merge, fair maxItems split with leftover redistribution, dedupe (no Apify import)
src/ats/               importable, Apify-free core
  refs.py              URL / "ats:slug" -> CompanyRef
  greenhouse.py lever.py ashby.py workday.py   one adapter per ATS (async generators of Job)
  http.py              RateLimitedClient (per-host concurrency, delay, retries, Retry-After, User-Agent)
  filters.py text.py models.py
.actor/                actor.json, input_schema.json, dataset_schema.json, output_schema.json, pay_per_event.json
tests/test_incremental.py   offline tests for onlyNewJobs (state, pruning, maxItems/budget leftovers, restarts, stateKey)
tests/test_incremental_state_integrity.py  offline tests: unconfirmed charges stay out of the state, Workday id stability, record-key collisions, entry cap
tests/test_pricing_shapes.py  offline tests: previous pricing (custom actor-start) and current pricing (apify-actor-start)
tests/test_unit.py     offline tests (URL parsing, filters, normalizers, retry logic, pipeline)
tests/test_fairness.py  offline tests (fair maxItems split, redistribution, SUMMARY.perCompany reasons, restarts)
tests/test_live.py     live-network tests (>=3 real companies per ATS)
```

Use the core without Apify:

```python
import asyncio
from src.ats import JobFilter, RateLimitedClient, parse_company
from src.pipeline import iter_jobs

async def run():
    refs = [parse_company(x) for x in ["greenhouse:stripe", "lever:spotify"]]
    async with RateLimitedClient() as client:
        async for job in iter_jobs(client, refs, JobFilter(keyword="engineer"), max_items=20):
            print(job.to_dict())

asyncio.run(run())
```

## Input

See `.actor/input_schema.json`.

* `companies` – career URLs or `ats:slug`: `greenhouse:stripe`, `lever:spotify`, `ashby:ramp`,
  `workday:nvidia.wd5/NVIDIAExternalCareerSite`. Auto-detected URL hosts: `boards.greenhouse.io`,
  `job-boards.greenhouse.io`, `jobs.lever.co` (+ `jobs.eu.lever.co`), `jobs.ashbyhq.com`, `*.myworkdayjobs.com`.
  Unparseable entries and failing companies are logged and reported in the `SUMMARY` key-value record; they never abort the run.
* `keyword` – matches **title + department** (case-insensitive). Spaces = AND, commas = OR.
* `location` – substring match against the primary and secondary locations. Comma = OR. The words of one alternative
  must all appear in the **same** location (`London Germany` does not match a job in `London, UK` + `Berlin, Germany`).
* `remoteOnly` – keep only jobs flagged remote (Lever/Ashby `workplaceType`, Workday `remoteType`, or "Remote" in a Greenhouse/Workday location).
* `maxItems` (default 100) – total cap, **split fairly across the companies**: while a company is still being read nobody
  may take more than an even share (`maxItems / companies`; with `maxItems >= companies` every company that has a matching
  job gets at least one). A company that ends with fewer jobs than its share (none, or an error) hands the leftover to
  the companies that still have jobs. Jobs are still pushed as soon as they are ready: a slow company never blocks the
  others from delivering their share, but its share stays reserved until it finishes, and the surplus of fast companies
  is held back meanwhile. A company that hangs therefore delays only the redistribution of its leftover (the HTTP
  layer's timeouts/`Retry-After` limit apply). Ready companies are served round-robin. The cap and the split hold across
  platform restarts (the jobs a company already delivered count toward its share). Spare slots of an uneven split are
  first come, first served.
* `onlyNewJobs` (default false) – incremental mode, see below.
* `stateKey` – optional name of the incremental memory; default is derived from the normalized company list.
* `includeDescription` (default true) – for Workday, `false` skips the per-job detail request (much faster).

## Incremental mode (`onlyNewJobs`)

State lives in the **named** key-value store `ats-job-feed-state` (named stores persist across runs; a run's default
store does not), in one record per state key: `STATE-<prefix>-<hash>` (see **Record key** below).

```json
{"v": 1, "jobs": {"greenhouse|stripe|123456": 20745, "lever|spotify|9f2c…": 20745}}
```

Keys are `ats|companySlug|jobId` (the same identity the restart logic uses), values are the day the job was last seen
(days since 1970-01-01, UTC). `src/incremental.py` owns the format. A job's identity must not depend on how it was fetched:
for Workday, `jobId` is derived from the list response only (a requisition-like list bullet, else the requisition id parsed
from the posting path, e.g. `…/Engineer_R123` gives `R123`, else the last path segment), never from the detail request, so
the same posting has the same id with `includeDescription` on or off and when a detail request fails.

* **Default key:** `companies-` + first 16 hex chars of a SHA-256 over the sorted, normalized company refs. Same set in
  any order, written as URL or `ats:slug`, gives the same key (slugs and Workday tenants are case-sensitive:
  `greenhouse:Stripe` and `greenhouse:stripe` are different keys); a different set starts a new memory. `stateKey` overrides it.
* **Record key:** always `STATE-<prefix>-<hash>`: the prefix is the state key with every character outside
  `[A-Za-z0-9!_.'()-]` replaced by `_` (cut so the whole key stays within 256 chars), the hash is 12 hex chars of a SHA-256
  of the *original* state key. Valid names get the hash too, so two different state keys never share a record, even when one
  looks like another's sanitized form.
* **Skip, don't charge:** `iter_jobs(known=..., on_known=...)` drops jobs that earlier runs delivered as they come off the
  per-company queue, before the fair-split bookkeeping: they use no `maxItems` slot and no fair share. `on_known`
  refreshes their last-seen day. Filters run inside the adapters, so they apply before this check.
* **Remembered only after a confirmed delivery:** a job is added to the state right after `push_data` succeeded (with
  charging on, after `charged_count > 0`). Jobs cut off by `maxItems` or the budget are not in the state, so the next run
  delivers them. Rows recovered from the dataset after a restart whose charge is unconfirmed (`SUMMARY.unbilledJobs`, see
  the write-then-charge gap below) are **never** written to the state: a later run may deliver (and charge) them again.
* **Size cap:** at most `MAX_ENTRIES` (200,000) identities are kept. Above that the least recently seen entries are evicted
  (on load, before anything is delivered, and on every save), a warning is logged and the count is reported as
  `SUMMARY.stateEvicted`. An evicted job that is still listed is delivered and charged again. Jobs still listed are
  refreshed every run, so eviction only reaches the stalest entries; a feed that keeps more than 200,000 distinct
  matching jobs should be split across several `stateKey`s.
* **Pruning:** entries older than `RETENTION_DAYS` (90) are dropped when the state is loaded and when it is saved (the cap above applies after pruning). A job
  that is still listed and matches the filters is refreshed every run and never expires.
* **Saving:** every 25 pushes (next to `JOBFEED_STATE`), on the SDK's `PERSIST_STATE` event (also emitted with
  `MIGRATING`), when the run ends or fails, and at startup after a restart. After a restart, the delivered keys recovered
  from the run's own state/dataset are written to the named state first, so a crash between the saves loses no mark.
* **Caveats:** only new job ids are detected (edited postings are not re-delivered, removals are not reported); two
  concurrent runs with the same key overwrite each other's marks (last save wins).

## Run summary (`SUMMARY` key-value record)

`jobsPushed`, `jobsPushedThisRun`, `resumed`, `stoppedReason` (`completed` | `maxItems` | `budget`), `onlyNewJobs`,
`newJobs`, `skippedSeenJobs`, `stateKey`, `stateStore`, `stateEvicted`, `perCompany`,
`perCompanyCounts`, `failedCompanies`, `invalidInputs`, `chargingEnabled`, `truncatedCompanies`, `detailFailures`,
`unverifiedJobsIncluded`, `unbilledJobs`.

`newJobs`, `skippedSeenJobs`, `stateKey`, `stateStore` and `stateEvicted` are `null` unless `onlyNewJobs` is on. `stateEvicted` is the number of remembered jobs dropped because the state exceeded its entry cap. `newJobs` is the number of
jobs delivered by the run (restarts included); `skippedSeenJobs` the number of already-delivered jobs met and skipped
(after a restart, the count of the final attempt, which re-reads the boards).

`perCompany` lists **every requested company** (valid input entries, in input order), including those with 0 jobs:

```json
"perCompany": {
  "greenhouse:stripe": {"count": 12, "reason": "cap_reached"},
  "lever:spotify":     {"count": 3,  "reason": "completed"},
  "ashby:ramp":        {"count": 0,  "reason": "no_matches"},
  "greenhouse:nope":   {"count": 0,  "reason": "error"}
}
```

The key is `<ats>:<slug>` (the same name as before; for Workday the tenant, `workday:nvidia`). `count` is the number of
jobs delivered over the whole run, restarts included. `reason`:

| `reason` | meaning |
|---|---|
| `completed` | the company's board was read to the end and it delivered jobs |
| `no_matches` | read to the end, nothing passed `keyword` / `location` / `remoteOnly` (`count` is 0) |
| `no_new_jobs` | incremental mode: read to the end, matching jobs exist but all were delivered by an earlier run (`count` is 0) |
| `cap_reached` | `maxItems` ended the run (or the company's share) before its board was seen to end. A company that never got a slot (`maxItems` < number of companies) is also `cap_reached` with `count` 0 |
| `error` | fetching failed (details in `failedCompanies`); jobs delivered before the failure are kept in `count` |
| `budget` | the run stopped at the max total charge before the board was seen to end |

If two entries share a name (two Workday sites of one tenant) they get one entry with the summed count and the strongest
reason (`error` > `budget` > `cap_reached` > `completed` > `no_new_jobs` > `no_matches`).

**Backward compatibility.** Until now `perCompany` was a flat map `{"<ats>:<slug>": <count>}` listing only companies with
at least one job. That flat map is still written as **`perCompanyCounts`** (same content as before), while `perCompany`
now has the object shape above. Consumers of the old `perCompany[name]` number should read `perCompanyCounts[name]` or
`perCompany[name].count`. The persisted `JOBFEED_STATE` record is unchanged (its `perCompany` stays a flat count map), so
runs resumed from an older state keep working.

## Output record

`title, company, location, remote, department, postedDate, applyUrl, atsSource, descriptionText, jobId, salary`
plus `jobUrl, employmentType, locations[], companySlug, departments[]`.

* `department` is the first department (string); `departments` lists **all** of them (Greenhouse jobs can belong to
  several; other ATS give a one-item list or `[]`). The `keyword` filter matches title + every department.
* Workday `remote`: an explicit `remoteType` (Remote / Hybrid / On-site) is authoritative; without it, a job is remote if
  any of its locations (primary or additional) contains "Remote".

* `postedDate`: ISO 8601. Greenhouse `first_published`, Lever `createdAt`, Ashby `publishedAt`; Workday uses the
  detail `startDate` (date only) or parses `postedOn` ("Posted 3 Days Ago" -> date). "Posted 30+ Days Ago" is open-ended and yields `null` when no detail is fetched.
* `salary`: `{min, max, currency, interval, summary}` when the ATS publishes it (Ashby compensation, Lever `salaryRange`, Greenhouse `pay_input_ranges`); otherwise `null`. Workday does not expose structured pay.
* `company`: Greenhouse supplies a real name; for the others it is derived from the slug/tenant (`ramp` -> `Ramp`).
* Workday `applyUrl` is the public job page (it contains the Apply button).

## Rate limiting

`RateLimitedClient`: max 4 concurrent requests per host, request starts per host spaced 150 ms apart (one evenly spaced
stream), up to 5 retries on 429/500/502/503/504 and transport errors with exponential backoff + jitter (our own backoff is capped at 60 s).
A 429/503, or a `Retry-After` (seconds or HTTP date) on one of those retried statuses, puts the **whole host** into a shared cool-down, so queued
requests to that host wait too. A server `Retry-After` is honoured in full; if it exceeds 15 minutes the request fails
(reported as a company error) instead of waiting. Descriptive `User-Agent`.

## Pay-per-event and `CHARGING_ENABLED`

Current pricing (`.actor/pay_per_event.json`): the platform-charged synthetic `apify-actor-start` ($0.00005) and the
primary event `job-scraped` ($0.0015 per job). The code only charges when the environment variable
**`CHARGING_ENABLED=1`** is set. `.actor/actor.json` sets it in `environmentVariables`, so charging is **on by default**
for the deployed actor (the code itself still defaults to off when the variable is absent, e.g. a bare local
`python -m src`):

* every job goes through `Actor.push_data(job, charged_event_name="job-scraped")`, which only pushes what the
  user's max total charge still allows;
* the custom `actor-start` event of the previous pricing ($0.01) is charged once per run **only if the run's current
  pricing still defines it with a price above zero** (`start_event_priced`), and never otherwise. The code therefore works
  under both pricing shapes, which matters because a price change is scheduled with a 14-day notice. When it is charged it
  is restart-safe (see below).

With charging on, the remaining capacity for `job-scraped` is checked **before any scraping** (after the custom start
fee, if any). If the budget is already exhausted nothing is fetched or pushed; during the run it stops as soon as a push
reports `charged_count == 0` / limit reached, so an unbillable job is never pushed. `SUMMARY.stoppedReason` is
`completed`, `maxItems` or `budget`. If the run is not pay-per-event, `CHARGING_ENABLED=1` only logs a warning.

> **Keep the built-in synthetic event `apify-default-dataset-item` OFF in the Console pricing.** Apify would charge it for
> every item on top of `job-scraped` (double charge). The actor logs a warning at startup if its pricing lists it, and
> `compute_push_data_limit` includes it in the budget. `apify-actor-start` is expected under the current pricing and is
> not flagged; the platform charges it itself, independent of `CHARGING_ENABLED`.

**Restarts.** Seen job keys, the delivered count, the start-charged flag and per-company counters are kept in the
key-value store record `JOBFEED_STATE` (`Actor.use_state`, persisted on `PERSIST_STATE`/`MIGRATING` and every 25 items).
A restarted run loads it, skips delivered jobs, never re-sends the start fee (it checks the platform's charged-event counts and a
`startChargeAttempted` flag persisted *before* the charge is sent) and counts `maxItems`/budget over the whole run. If the dataset is ahead of the saved state
(crash between persists), job keys are rebuilt from the dataset.

**Write-then-charge gap and the billing policy.** `push_data(..., charged_event_name=...)` writes the dataset row first
and charges second, so a crash in between leaves a delivered, unbilled row. The policy is: **an ambiguous charge outcome
is never retried in-process; under-billing is preferred over double billing.** Concretely:

* Before every charged push the job key is saved to the KV record `JOBFEED_PENDING`.
* If a push or charge raises, the error propagates and the run fails. There is no in-process re-charge: the platform may
  have accepted the charge while its response was lost, and a retry (with no idempotency key) would bill the same row twice.
* **A restart never re-sends an ambiguous charge either.** The restarted SDK's charged count is only a snapshot: a timed-out
  request can still be executing remotely and commit after it, so the count cannot prove a charge did *not* happen. A pending
  key found in the dataset is therefore counted as delivered and, unless the startup charged count already covers every row,
  listed in `SUMMARY.unbilledJobs` as *possibly unbilled*. It is not charged.
* The custom `actor-start` fee (previous pricing only) follows the same rule: `startChargeAttempted=true` is saved in `JOBFEED_STATE` (and persisted)
  *before* the charge is sent. A restart skips the start charge if that flag is set or the platform's count is > 0. A charge
  refused for lack of budget (`charged_count == 0`) is definitive, so it clears the flag.
* Remaining (accepted) risk: **under-billing only**. A charge that failed before reaching the platform, or a run that dies
  between the dataset write and the charge, leaves a delivered row (or the start fee) unbilled; the rows are listed in
  `SUMMARY.unbilledJobs` and a start fee that was attempted but unconfirmed is only logged. Billed events never exceed
  delivered rows and the start fee is never billed twice. Cost: one extra KV write per job.

With `CHARGING_ENABLED=1` on a pay-per-event run, the actor refuses to start unless `job-scraped` has a price.

Without the variable, nothing is charged by the code (so local/dev runs and non-PPE deployments are unaffected). Locally the
SDK ignores charges anyway ("Actor does not use the pay-per-event pricing").
Pricing itself is configured in the Apify Console – the JSON file is the source of truth to paste/sync from.

## Memory

`defaultMemoryMbytes` is 1024 (min 256, max 1024). Jobs stream: each company feeds a bounded queue (5 items) and every
job is pushed as soon as it is ready. Workday is read page by page. Greenhouse, Lever and Ashby return a whole board in
one response, which is the largest allocation: a synthetic Greenhouse board of 2,000 jobs with 20 KB descriptions peaked at
about 105 MB RSS, and ten such boards fetched concurrently (about 10,000 jobs / 200 MB of JSON) at about 260 MB. The
incremental state is a few dozen bytes per job and capped at 200,000 entries (a few tens of MB at most to load and save). So `maxItems` of 5,000 with descriptions
fits comfortably; the memory driver is the number of very large boards read in parallel, not `maxItems`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # apify httpx pytest pytest-asyncio
pytest tests/test_unit.py                    # offline
pytest tests/test_live.py                    # hits real ATS endpoints (~1 min)
pytest -m "not live"                         # everything offline

# run the actor locally (reads storage/key_value_stores/default/INPUT.json; charging off unless the env var is set)
APIFY_LOCAL_STORAGE_DIR=$PWD/storage python -m src      # or: apify run
CHARGING_ENABLED=1 APIFY_LOCAL_STORAGE_DIR=$PWD/storage python -m src   # exercise the charge path
```

Live tests depend on third-party boards; if a company closes its board or changes ATS, swap it in `COMPANIES`
in `tests/test_live.py`.

## Limitations

* Only public boards. Workday sites hosted on `*.myworkdaysite.com` are not supported.
* Greenhouse/Lever/Ashby return the full board in one response; filtering is applied client-side.
* Workday search results are capped at 3,000 postings per search (150 pages × 20). If a tenant has more, the crawl stops,
  a warning is logged and `SUMMARY.truncatedCompanies` names the company; narrow the search with `keyword`.
* Workday detail requests that fail are logged and counted in `SUMMARY.detailFailures`. The job is kept (without
  description); a filter fact only the detail had (multi-location list, remote type) is treated as unknown, not as a
  mismatch, and such jobs are counted in `SUMMARY.unverifiedJobsIncluded`.
* Workday's list view hides locations behind "N Locations" and sometimes omits `remoteType`; when a location/remote filter needs it the detail is fetched.
* Incremental mode and unconfirmed charges: after a restart, a row that is in the dataset but whose charge could not be
  confirmed is listed in `SUMMARY.unbilledJobs` and is deliberately **not** remembered in the named state, so a later run
  may deliver it again (and charge it then). That is the accepted trade-off: the job is never skipped unpaid. Ambiguous
  charges are still never re-sent within the run, and there is no idempotency-key reconciliation.
* Incremental state holds at most 200,000 jobs per `stateKey`; beyond that the least recently seen are evicted and may be
  delivered again (`SUMMARY.stateEvicted`).
* Not published or deployed from this repo's automation: push/`apify push` is left to the maintainer.
