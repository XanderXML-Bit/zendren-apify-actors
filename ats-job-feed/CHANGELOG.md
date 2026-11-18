# Changelog

## 2026-10-19

Relaunch.

- New pricing: $0.0015 per job delivered plus $0.00005 per run to start (at the default 1,024 MB).
- New **New jobs only** mode (`onlyNewJobs`, `stateKey`): later runs deliver, and charge for, only postings not delivered before.
- Default memory is now 1,024 MB.
- Workday job ids are stable, with or without descriptions, so a job is not delivered twice.

## 2026-10-16

First public version: Greenhouse, Lever, Ashby and Workday jobs in one normalized schema, with keyword, location and remote filters.
