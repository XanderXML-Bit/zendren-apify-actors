"""Apify entrypoint: ATS job feed.

Charging: the pay-per-event charge ``job-scraped`` (per pushed item, the primary event)
is only issued when the environment variable ``CHARGING_ENABLED=1``. The synthetic
``apify-actor-start`` event is charged by the platform itself and is expected; the
synthetic ``apify-default-dataset-item`` event must stay OFF (it would double-charge
every item). The custom ``actor-start`` event belonged to the previous pricing: it is
charged once per run only while the run's current pricing still defines it with a price
above zero, and never otherwise, so the code works under both pricings.

With charging on, the run never scrapes or pushes beyond the user's max charge: the
remaining capacity for ``job-scraped`` is checked before any work, and every item goes
through the charge-aware ``push_data(..., charged_event_name=...)``.

Restarts (platform migration): seen job keys, the pushed count and the start-charged
flag live in the key-value store (``use_state``), so a restarted run skips delivered
jobs, does not charge the start fee again and honours ``maxItems`` across restarts.

Incremental mode (``onlyNewJobs``): the identities of delivered jobs live in a named
key-value store (see :mod:`src.incremental`), so the next run delivers and charges only
jobs it has not delivered before. A job is remembered only after it was pushed (and
charged); the state is saved periodically, on ``PERSIST_STATE``/migration and at the end.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import os

from apify import Actor, Event

from .ats import JobFilter, RateLimitedClient, RunReport, parse_company
from .incremental import STORE_NAME, SeenState, default_state_key, record_key
from .pipeline import COMPLETED, ERRORED, iter_jobs, job_key

DEFAULT_MAX_ITEMS = 100
ENV_CHARGING = "CHARGING_ENABLED"
START_EVENT = "actor-start"  # previous pricing only; charged just while the run's pricing defines it
JOB_EVENT = "job-scraped"
# Built-in events that must stay OFF. ``apify-actor-start`` is not listed: it is part of the pricing.
DOUBLE_CHARGE_EVENTS = ("apify-default-dataset-item",)
STATE_KEY = "JOBFEED_STATE"
# Written *before* every charged push: {"pending": <job key or None>}. See _note_pending_unbilled.
PENDING_KEY = "JOBFEED_PENDING"
PERSIST_EVERY = 25

# SUMMARY.perCompany[<ats:slug>].reason: why a company stopped contributing jobs.
R_COMPLETED = "completed"      # its board was read to the end and it delivered jobs
R_NO_MATCHES = "no_matches"    # read to the end, nothing passed the filters (count 0)
R_NO_NEW = "no_new_jobs"       # incremental mode: read to the end, every matching job was seen before
R_CAP = "cap_reached"          # maxItems ended the run (or its share) before its board was seen to end
R_ERROR = "error"              # fetching failed (see failedCompanies); jobs delivered before it are kept
R_BUDGET = "budget"            # the max total charge ended the run before its board was seen to end
# when two requested entries share a name (two Workday sites of one tenant) the strongest wins
_REASON_ORDER = (R_ERROR, R_BUDGET, R_CAP, R_COMPLETED, R_NO_NEW, R_NO_MATCHES)


def company_name(ref) -> str:
    """Name used for per-company counts; equals ``f"{job.atsSource}:{job.companySlug}"``."""
    return f"{ref.ats}:{ref.slug}"


def per_company_summary(refs, counts: dict[str, int], outcomes: dict[str, str],
                        failed: dict, stopped: str, skipped: dict[str, int] | None = None) -> dict[str, dict]:
    """``{name: {"count": N, "reason": ...}}`` for every requested company, in input order.

    ``outcomes`` is what the pipeline saw this process (ref.key -> completed/error/stopped);
    a company without an outcome was never reached (the run was already capped or out of
    budget at start) and gets the reason the run ended with. ``skipped`` (incremental mode)
    is how many already-delivered jobs each company had; a finished company that delivered
    nothing but skipped some is ``no_new_jobs`` rather than ``no_matches``."""
    out: dict[str, dict] = {}
    for ref in refs:
        name = company_name(ref)
        count = counts.get(name, 0)
        outcome = outcomes.get(ref.key)
        if outcome == ERRORED or (outcome is None and ref.key in failed):
            reason = R_ERROR
        elif outcome == COMPLETED:
            reason = R_COMPLETED if count else R_NO_NEW if (skipped or {}).get(name) else R_NO_MATCHES
        else:  # stopped early, or never started
            reason = R_BUDGET if stopped == "budget" else R_CAP
        prev = out.get(name)
        if prev and _REASON_ORDER.index(prev["reason"]) < _REASON_ORDER.index(reason):
            reason = prev["reason"]
        out[name] = {"count": count, "reason": reason}
    return out


def charging_enabled() -> bool:
    return os.environ.get(ENV_CHARGING, "0").strip() == "1"


def _entries(raw) -> list[str]:
    """Accept strings, comma/newline separated strings, or {'url': ...} objects."""
    out: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            item = item.get("url") or item.get("company") or ""
        if isinstance(item, str):
            out += [p.strip() for p in item.replace("\n", ",").split(",") if p.strip()]
    return out


def enabled_synthetic_events(prices) -> list[str]:
    """Built-in events that look switched on in the pricing but must be OFF."""
    return [e for e in DOUBLE_CHARGE_EVENTS if e in (prices or {})]


def start_event_priced(prices) -> bool:
    """True if the run's pricing defines the custom ``actor-start`` event with a price above zero."""
    return bool((prices or {}).get(START_EVENT))


def _new_state() -> dict:
    return {"seen": [], "pushed": 0, "startCharged": False, "perCompany": {},
            "failed": {}, "notes": {}, "startChargeAttempted": False, "skippedSeen": 0}


async def _persist(actor) -> None:
    kvs = await actor.open_key_value_store()
    await kvs.persist_autosaved_values()


def _row_key(item: dict) -> str:
    return f"{item.get('atsSource')}|{item.get('companySlug') or ''}|{item.get('jobId')}"


async def _scan_dataset(actor) -> tuple[list[str], dict[str, int]]:
    """Job keys (dataset order) and per-company counts of everything delivered so far."""
    dataset = await actor.open_dataset()
    keys: list[str] = []
    per_company: dict[str, int] = {}
    async for item in dataset.iterate_items(fields=["atsSource", "companySlug", "jobId"]):
        keys.append(_row_key(item))
        name = f"{item.get('atsSource')}:{item.get('companySlug')}"
        per_company[name] = per_company.get(name, 0) + 1
    return keys, per_company


async def _recover_from_dataset(actor, state: dict, seen: set[str], *, force: bool = False) -> list[str] | None:
    """State is persisted periodically, so a crash can leave jobs in the dataset
    that the state does not know. Rebuild their keys, the pushed count and the
    per-company counts from the dataset. Returns the dataset's keys when scanned."""
    dataset = await actor.open_dataset()
    info = await dataset.get_metadata()
    if not force and (info.item_count or 0) <= state["pushed"]:
        return None
    if (info.item_count or 0) > state["pushed"]:
        actor.log.warning("Dataset has %d items but saved state only %d; rebuilding from the dataset.",
                          info.item_count, state["pushed"])
    keys, per_company = await _scan_dataset(actor)
    for key in keys:
        if key not in seen:
            seen.add(key)
            state["seen"].append(key)
    state["pushed"] = max(state["pushed"], len(keys))
    if per_company:
        state["perCompany"] = per_company  # the dataset is the source of truth
    return keys


async def _note_pending_unbilled(actor, cm, state: dict, keys: list[str] | None = None) -> None:
    """Restart only: a pending row found in the dataset is delivered, but its charge is ambiguous.

    ``Actor.push_data(..., charged_event_name=...)`` writes the dataset first and charges
    second, and before each push we persist its key (``PENDING_KEY``). If that key is in the
    dataset, the charge may or may not have been accepted (a lost response, a request still
    executing remotely and committing after our startup snapshot). The strict policy is that
    an ambiguous charge is NEVER re-sent, here either: the row counts as delivered and is
    listed in ``SUMMARY.unbilledJobs`` as possibly unbilled. Only when the startup charged
    count already covers every row is it known to be paid (counts can lag, never lead).
    """
    marker = await actor.get_value(PENDING_KEY) or {}
    pending = marker.get("pending")
    if not pending:
        return
    if keys is None:
        keys, _ = await _scan_dataset(actor)
    if pending not in keys:  # crashed before the write: the job is simply re-delivered
        return
    if cm.get_charged_event_count(JOB_EVENT) >= len(keys):
        return  # the charge is confirmed
    actor.log.warning("Job %s is in the dataset but its charge is unconfirmed; it is NOT charged again "
                      "(possibly unbilled).", pending)
    state["notes"].setdefault("unbilledJobs", {})[pending] = "delivered, charge outcome unconfirmed (possibly unbilled)"


def _merge_notes(base: dict, report: RunReport) -> dict:
    """Notes of earlier attempts plus this process's report (recomputed from ``base``
    each time, so saving repeatedly never double counts). Truncation messages are
    unioned per company; counters add up. A posting re-crawled after a restart whose
    detail request fails again is counted again (the counters count attempts)."""
    cur = report.to_dict()
    out = copy.deepcopy(base)
    trunc = out.setdefault("truncatedCompanies", {})
    for key, msg in cur["truncatedCompanies"].items():
        old = trunc.get(key)
        trunc[key] = msg if not old else old if msg in old else f"{old}; {msg}"
    for field in ("detailFailures", "unverifiedJobsIncluded"):
        counts = out.setdefault(field, {})
        for key, n in cur[field].items():
            counts[key] = counts.get(key, 0) + n
    return out


async def run(actor=Actor) -> None:
    """The actor logic; ``actor`` is the (initialised) Apify ``Actor``."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = actor.log
    inp = await actor.get_input() or {}
    charge = charging_enabled()
    log.info("Pay-per-event charging %s (%s=%s)", "ENABLED" if charge else "disabled",
             ENV_CHARGING, os.environ.get(ENV_CHARGING, "unset"))

    cm = actor.get_charging_manager()
    pricing = cm.get_pricing_info()
    ppe = bool(pricing.is_pay_per_event)
    if ppe:
        on = enabled_synthetic_events(pricing.per_event_prices)
        if on:
            log.warning(
                "Built-in pricing event(s) %s look ENABLED. They are charged by the platform regardless of "
                "%s and would double-charge with %s. Turn them OFF in the Console pricing for this Actor.",
                ", ".join(on), ENV_CHARGING, JOB_EVENT)
    if charge and not ppe:
        log.warning("%s=1 but this run does not use pay-per-event pricing: nothing is charged and no "
                    "budget limit is enforced.", ENV_CHARGING)
    if charge and ppe:
        if not (pricing.per_event_prices or {}).get(JOB_EVENT):
            # the SDK would deliver items and "charge" them at zero: refuse instead
            msg = (f"{ENV_CHARGING}=1 but pay-per-event pricing has no price for "
                   f"{JOB_EVENT!r}; refusing to deliver unbilled jobs.")
            log.error(msg)
            await actor.fail(status_message=msg)
            return
    metered = charge and ppe  # budget enforcement + charge-aware pushes
    # the custom start event exists only under the previous pricing; under the current one the
    # platform charges ``apify-actor-start`` itself and there is nothing for us to charge
    charge_start = metered and start_event_priced(pricing.per_event_prices)

    refs, bad = [], []
    for entry in _entries(inp.get("companies")):
        try:
            ref = parse_company(entry)
        except ValueError as exc:
            log.error("Skipping %r: %s", entry, exc)
            bad.append(entry)
            continue
        if ref not in refs:
            refs.append(ref)
    if not refs:
        msg = "No valid companies in input."
        await actor.set_value("SUMMARY", {"jobsPushed": 0, "error": msg, "perCompany": {}, "invalidInputs": bad})
        await actor.fail(status_message=msg)
        return

    flt = JobFilter(
        keyword=(inp.get("keyword") or "").strip() or None,
        location=(inp.get("location") or "").strip() or None,
        remote_only=bool(inp.get("remoteOnly", False)),
    )
    max_items = int(inp.get("maxItems") or DEFAULT_MAX_ITEMS)
    include_description = bool(inp.get("includeDescription", True))

    only_new = bool(inp.get("onlyNewJobs", False))
    seen_state: SeenState | None = None
    state_key: str | None = None
    if only_new:
        state_key = (str(inp.get("stateKey") or "")).strip() or default_state_key(refs)
        kvs = await actor.open_key_value_store(name=STORE_NAME)
        seen_state = SeenState(kvs, record_key(state_key))
        await seen_state.load()
        log.info("Incremental mode: state %r in store %r (%s, %d jobs remembered, %d expired, %d evicted).",
                 state_key, STORE_NAME, "found" if seen_state.loaded else "none yet, first run",
                 len(seen_state.known), seen_state.pruned, seen_state.evicted)

    # ---- restart-safe state --------------------------------------------------
    state = await actor.use_state(_new_state(), key=STATE_KEY)
    for k, v in _new_state().items():
        state.setdefault(k, v)
    seen: set[str] = set(state["seen"])
    resumed = bool(state["pushed"] or state["startCharged"])
    marker = await actor.get_value(PENDING_KEY) if metered else None
    keys = await _recover_from_dataset(actor, state, seen, force=bool(marker and marker.get("pending")))
    if metered:
        await _note_pending_unbilled(actor, cm, state, keys)
        if state["notes"].get("unbilledJobs"):
            await _persist(actor)  # the warning must be durable before its marker is cleared
        await actor.set_value(PENDING_KEY, {"pending": None})
    resumed = resumed or bool(state["pushed"])
    base_notes = copy.deepcopy(state["notes"])  # what earlier attempts of this run reported
    if resumed:
        log.info("Resuming: %d jobs already delivered, start fee charged: %s",
                 state["pushed"], state["startCharged"])
    if seen_state is not None and state["seen"]:
        # delivered by an earlier attempt of this run (the dataset is the truth): make sure they are
        # remembered, except rows whose charge is unconfirmed (SUMMARY.unbilledJobs): those never
        # enter the named state, so a later run may deliver them again rather than skip them unpaid
        unconfirmed = set(state["notes"].get("unbilledJobs") or {})
        seen_state.mark_all(k for k in state["seen"] if k not in unconfirmed)
        await seen_state.save()

    stopped = "completed"
    budget_capped = False
    this_run = 0
    remaining = max_items - state["pushed"]
    if remaining <= 0:
        stopped = "maxItems"

    # ---- start fee (once per run, also across restarts) ------------------------
    if charge_start and remaining > 0 and not state["startCharged"]:
        if cm.get_charged_event_count(START_EVENT) > 0:
            state["startCharged"] = True  # charged before a restart that lost our flag
        elif state["startChargeAttempted"]:
            # an earlier process sent the charge and died before knowing the outcome: it may
            # still commit remotely, so it is never re-sent (the fee may go unbilled)
            log.warning("The %r charge of an earlier attempt has an unconfirmed outcome; "
                        "not charging it again.", START_EVENT)
        else:
            # persisted BEFORE the charge, so a restart can tell "maybe sent" from "never sent"
            state["startChargeAttempted"] = True
            await _persist(actor)
            res = await actor.charge(START_EVENT)
            if res.charged_count == 0:  # definitive: refused, nothing was billed
                state["startChargeAttempted"] = False
                log.warning("Max total charge too low for the %r event; not scraping.", START_EVENT)
                stopped = "budget"
                remaining = 0
            else:
                state["startCharged"] = True
        await _persist(actor)

    # ---- remaining budget for job-scraped, before any scraping ----------------
    if metered and remaining > 0:
        capacity = cm.compute_push_data_limit(remaining, JOB_EVENT, is_default_dataset=True)
        if capacity <= 0:
            log.warning("No budget left for %r (max total charge reached); not scraping.", JOB_EVENT)
            stopped = "budget"
            remaining = 0
        elif capacity < remaining:
            log.info("Budget allows only %d more jobs (requested %d).", capacity, remaining)
            remaining = capacity
            budget_capped = True

    report = RunReport()

    def on_error(ref, exc) -> None:
        state["failed"][ref.key] = f"{type(exc).__name__}: {exc}"

    def sync_notes() -> None:
        # rebuilt from base_notes each time, so repeated calls never double count
        state["notes"] = _merge_notes(base_notes, report)

    report.on_change = sync_notes  # the autosaved state follows every report change

    async def push_metered(job):
        # persisted before the write: if the run dies after the dataset write but before the
        # charge is confirmed, the next start finds this key and lists the row in
        # SUMMARY.unbilledJobs (possibly unbilled). A failed push/charge is never re-sent,
        # in this process or after a restart: its outcome is ambiguous (the platform may have
        # accepted it, or still be executing it), so it propagates and the run fails.
        await actor.set_value(PENDING_KEY, {"pending": job_key(job)})
        return await actor.push_data(job.to_dict(), charged_event_name=JOB_EVENT)

    outcomes: dict[str, str] = {}
    # incremental mode: jobs of earlier runs met again this process (counted once each)
    skipped_keys: set[str] = set()
    skipped_by_company: dict[str, int] = {}

    def on_known(job) -> None:
        key = job_key(job)
        if key in skipped_keys:
            return
        skipped_keys.add(key)
        seen_state.touch(key)
        name = f"{job.atsSource}:{job.companySlug}"
        skipped_by_company[name] = skipped_by_company.get(name, 0) + 1

    async def save_seen_state(*_) -> None:
        if seen_state is not None:
            await seen_state.save()

    # jobs each company already delivered before a restart count toward its fair share
    # (counts are per name; of refs sharing a name the first carries the whole count)
    prior: list[int] = []
    claimed_names: set[str] = set()
    for ref in refs:
        name = company_name(ref)
        prior.append(0 if name in claimed_names else state["perCompany"].get(name, 0))
        claimed_names.add(name)

    if remaining > 0:
        if seen_state is not None and hasattr(actor, "on"):
            actor.on(Event.PERSIST_STATE, save_seen_state)  # also emitted together with MIGRATING
        try:
            async with RateLimitedClient() as client:
                # aclosing: leaving the loop (budget/maxItems) must cancel and await the
                # worker tasks *before* the client they use is closed
                async with contextlib.aclosing(iter_jobs(
                    client, refs, flt,
                    include_description=include_description,
                    max_items=remaining,
                    on_error=on_error,
                    seen=seen,
                    report=report,
                    prior=prior,
                    outcomes=outcomes,
                    known=seen_state.known if seen_state is not None else None,
                    on_known=on_known,
                )) as jobs:
                    async for job in jobs:
                        if metered:
                            res = await push_metered(job)
                            if res.charged_count == 0:  # not pushed: nothing left to bill
                                log.warning("Max total charge reached; stopping.")
                                stopped = "budget"
                                break
                        else:
                            await actor.push_data(job.to_dict())
                        state["seen"].append(job_key(job))
                        if seen_state is not None:
                            seen_state.mark(job_key(job))  # only now: it was pushed (and charged)
                        state["pushed"] += 1
                        this_run += 1
                        name = f"{job.atsSource}:{job.companySlug}"
                        state["perCompany"][name] = state["perCompany"].get(name, 0) + 1
                        if metered and res.event_charge_limit_reached:
                            log.warning("Max total charge reached; stopping.")
                            stopped = "budget"
                            break
                        if state["pushed"] % PERSIST_EVERY == 0:
                            sync_notes()
                            await actor.set_status_message(f"{state['pushed']} jobs scraped")
                            await _persist(actor)
                            await save_seen_state()
        finally:
            if seen_state is not None and hasattr(actor, "off"):
                actor.off(Event.PERSIST_STATE, save_seen_state)
            await save_seen_state()  # also when the run dies: what was pushed must be remembered
        for ref in refs:  # a company that now ran to the end no longer counts as failed
            if outcomes.get(ref.key) == COMPLETED:
                state["failed"].pop(ref.key, None)
        if stopped == "completed" and state["pushed"] >= max_items:
            stopped = "maxItems"
        elif stopped == "completed" and budget_capped and this_run >= remaining:
            stopped = "budget"

    sync_notes()
    notes = state["notes"]
    if metered:
        await actor.set_value(PENDING_KEY, {"pending": None})
    for key, msg in notes.get("truncatedCompanies", {}).items():
        log.warning("INCOMPLETE %s: %s", key, msg)
    skipped_total = max(state["skippedSeen"], len(skipped_keys))
    state["skippedSeen"] = skipped_total
    summary = {
        "jobsPushed": state["pushed"],
        "jobsPushedThisRun": this_run,
        "resumed": resumed,
        "stoppedReason": stopped,
        "onlyNewJobs": only_new,
        # incremental mode only (null otherwise): jobs delivered now, jobs skipped because an
        # earlier run delivered them (at least this many after a restart), and where the state lives
        "newJobs": state["pushed"] if only_new else None,
        "skippedSeenJobs": skipped_total if only_new else None,
        "stateKey": state_key,
        "stateStore": STORE_NAME if only_new else None,
        # incremental mode only: remembered jobs dropped because the state exceeded its entry cap
        # (they are delivered again if still listed); null otherwise
        "stateEvicted": seen_state.evicted if seen_state is not None else None,
        "perCompany": per_company_summary(refs, state["perCompany"], outcomes, state["failed"], stopped,
                                          skipped_by_company),
        "perCompanyCounts": dict(state["perCompany"]),
        "failedCompanies": dict(state["failed"]),
        "invalidInputs": bad,
        "chargingEnabled": charge,
        "truncatedCompanies": notes.get("truncatedCompanies", {}),
        "detailFailures": notes.get("detailFailures", {}),
        "unverifiedJobsIncluded": notes.get("unverifiedJobsIncluded", {}),
        "unbilledJobs": notes.get("unbilledJobs", {}),
    }
    await _persist(actor)
    await actor.set_value("SUMMARY", summary)
    log.info("Done: %s", summary)
    if only_new:
        msg = (f"{state['pushed']} new jobs from {len(state['perCompany'])} companies "
               f"({skipped_total} already seen, skipped)")
    else:
        msg = f"Scraped {state['pushed']} jobs from {len(state['perCompany'])} companies"
    if stopped == "budget":
        msg += "; stopped at max total charge"
    if state["failed"]:
        msg += f"; {len(state['failed'])} company errors"
    if notes.get("truncatedCompanies"):
        msg += f"; {len(notes['truncatedCompanies'])} truncated"
    await actor.set_status_message(msg)


async def main() -> None:
    async with Actor:
        await run(Actor)
