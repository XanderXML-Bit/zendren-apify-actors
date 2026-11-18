"""Apify entrypoint: Greenhouse job feed.

Charging: the pay-per-event ``job`` event is only charged when the environment variable
``CHARGING_ENABLED=1`` (set in ``.actor/actor.json``). The platform charges the synthetic
``apify-actor-start`` event itself; the synthetic ``apify-default-dataset-item`` event must
stay OFF in the Console pricing (it would double charge every job).

With charging on, the run never fetches or pushes beyond the user's max charge: the remaining
capacity for ``job`` is checked before any work, and every item goes through the charge-aware
``push_data(..., charged_event_name=...)``.

Restarts (platform migration): delivered job keys, the pushed count and per-board counts live in
the key-value store (``use_state``), so a restarted run skips delivered jobs, never charges one
twice and honours ``maxItems`` across restarts. An ambiguous charge (crash between the dataset
write and the charge) is never re-sent; the job is listed in ``SUMMARY.unbilledJobs`` and is
NOT written into the named incremental history (so a later ``onlyNewJobs`` run delivers it again).

Incremental mode (``onlyNewJobs``): see :mod:`src.incremental`. A job is marked seen only after
it was pushed and its charge was confirmed. Overlapping runs with the same ``stateKey`` are not
supported; a best-effort run lock refuses to start next to a live run.

If paid charging is active, an enabled ``apify-default-dataset-item`` price makes the run fail
before it fetches anything: the platform would bill it on top of every ``job`` charge.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import os

from apify import Actor

from . import incremental
from .greenhouse import CONTENT_MODES, DATE_FIELDS, JobFilter, Options, parse_updated_since
from .http import RateLimitedClient
from .models import BoardRef, RunReport
from .pipeline import COMPLETED, ERRORED, iter_jobs
from .refs import parse_board

DEFAULT_MAX_ITEMS = 100
ENV_CHARGING = "CHARGING_ENABLED"
JOB_EVENT = "job"
ITEM_EVENT = "apify-default-dataset-item"            # must be OFF (it would bill on top of every job)
SYNTHETIC_EVENTS = (ITEM_EVENT,)                     # apify-actor-start is the platform's and stays allowed
ENV_RUN_ID = "ACTOR_RUN_ID"
STATE_KEY = "GHFEED_STATE"
# Written *before* every charged push: {"pending": <job key or None>}. See _note_pending_unbilled.
PENDING_KEY = "GHFEED_PENDING"
TREES_KEY = "DEPARTMENTS_OFFICES"
PERSIST_EVERY = 20

# SUMMARY.boards[<token>].reason: why a board stopped contributing jobs.
R_COMPLETED = "completed"      # its board was read to the end and it delivered jobs
R_NO_MATCHES = "no_matches"    # read to the end, nothing new passed the filters (count 0)
R_CAP = "cap_reached"          # maxItems ended the run (or its share) before its board was seen to end
R_ERROR = "error"              # fetching failed (see SUMMARY.errors); jobs delivered before it are kept
R_BUDGET = "budget"            # the max total charge ended the run before its board was seen to end


def charging_enabled() -> bool:
    return os.environ.get(ENV_CHARGING, "0").strip() == "1"


def _entries(raw) -> list[str]:
    """Accept strings, comma/newline separated strings, or {'url': ...} objects."""
    out: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            item = item.get("url") or item.get("board") or ""
        if isinstance(item, str):
            out += [p.strip() for p in item.replace("\n", ",").split(",") if p.strip()]
    return out


def _names(raw) -> list[str]:
    """Filter names: one per list item (names may contain commas); newlines also separate."""
    out: list[str] = []
    for item in raw or []:
        if isinstance(item, str):
            out += [p.strip() for p in item.splitlines() if p.strip()]
    return out


def enabled_synthetic_events(prices) -> list[str]:
    """Built-in events with a price above zero in the pricing (should be OFF)."""
    return [e for e in SYNTHETIC_EVENTS if (prices or {}).get(e) and prices[e] > 0]


def per_board_summary(refs, counts: dict[str, int], outcomes: dict[str, str], errors: dict,
                      report_notes: dict, stopped: str) -> dict[str, dict]:
    """``{token: {name, count, reason, jobsOnBoard, matchedFilters}}`` for every requested board, in input order.

    ``outcomes`` is what the pipeline saw this process (token -> completed/error/stopped); a board
    without an outcome was never reached and gets the reason the run ended with."""
    out: dict[str, dict] = {}
    for ref in refs:
        count = counts.get(ref.token, 0)
        outcome = outcomes.get(ref.key)
        if outcome == ERRORED or (outcome is None and ref.key in errors):
            reason = R_ERROR
        elif outcome == COMPLETED:
            reason = R_COMPLETED if count else R_NO_MATCHES
        else:  # stopped early, or never started
            reason = R_BUDGET if stopped == "budget" else R_CAP
        out[ref.token] = {
            "name": report_notes["names"].get(ref.token),
            "count": count,
            "reason": reason,
            "jobsOnBoard": report_notes["onBoard"].get(ref.token),
            "matchedFilters": report_notes["matched"].get(ref.token),
        }
    return out


async def _fail(actor, msg: str, **summary) -> None:
    """Fail the run before any job was delivered, still writing a SUMMARY (the README promises one per run)."""
    actor.log.error("%s", msg)
    await actor.set_value("SUMMARY", {"jobsPushed": 0, "error": msg, "boards": {}, **summary})
    await actor.fail(status_message=msg)


def _new_state() -> dict:
    return {"seen": [], "pushed": 0, "perBoard": {}, "errors": {}, "notes": {}}


async def _persist(actor) -> None:
    kvs = await actor.open_key_value_store()
    await kvs.persist_autosaved_values()


def _confirmed(state: dict) -> list[str]:
    """Delivered keys whose charge is confirmed: the only ones that may enter the named incremental
    history. Rows listed in ``notes.unbilledJobs`` were delivered but possibly never billed, so a
    later onlyNewJobs run must deliver (and charge) them again."""
    unbilled = state["notes"].get("unbilledJobs") or {}
    return [k for k in state["seen"] if k not in unbilled]


def _row_key(item: dict) -> str:
    return f"{item.get('boardToken')}|{item.get('jobId')}"


async def _scan_dataset(actor) -> tuple[list[str], dict[str, int]]:
    """Job keys (dataset order) and per-board counts of everything delivered so far."""
    dataset = await actor.open_dataset()
    keys: list[str] = []
    per_board: dict[str, int] = {}
    async for item in dataset.iterate_items(fields=["boardToken", "jobId"]):
        keys.append(_row_key(item))
        token = item.get("boardToken")
        per_board[token] = per_board.get(token, 0) + 1
    return keys, per_board


async def _recover_from_dataset(actor, state: dict, seen: set[str], *, force: bool = False) -> list[str] | None:
    """State is persisted periodically, so a crash can leave jobs in the dataset that the state
    does not know. Rebuild their keys, the pushed count and the per-board counts from the dataset.
    Returns the dataset's keys when scanned."""
    dataset = await actor.open_dataset()
    info = await dataset.get_metadata()
    if not force and (info.item_count or 0) <= state["pushed"]:
        return None
    if (info.item_count or 0) > state["pushed"]:
        actor.log.warning("Dataset has %d items but saved state only %d; rebuilding from the dataset.",
                          info.item_count, state["pushed"])
    keys, per_board = await _scan_dataset(actor)
    for key in keys:
        if key not in seen:
            seen.add(key)
            state["seen"].append(key)
    state["pushed"] = max(state["pushed"], len(keys))
    if per_board:
        state["perBoard"] = per_board  # the dataset is the source of truth
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
    """Notes of earlier attempts plus this process's report (recomputed from ``base`` each time,
    so saving repeatedly never double counts). Per-board facts of this process win; failed
    detail requests are unioned."""
    cur = report.notes()
    out = copy.deepcopy(base)
    for field in ("names", "onBoard", "matched", "alreadySeen", "unmatchedFilters", "hierarchyUnavailable"):
        out.setdefault(field, {}).update(cur[field])
    failures = out.setdefault("detailFailures", {})
    for token, ids in cur["detailFailures"].items():
        failures[token] = sorted(set(failures.get(token, [])) | set(ids), key=str)
    return out



def _run_status_getter(actor):
    """``async f(run_id) -> status string | None`` through the Apify client; ``None`` when no client is available."""
    try:
        client = actor.apify_client
    except Exception:  # no token / not on the platform (local runs)
        return None
    if client is None:
        return None

    async def get_status(run_id: str) -> str | None:
        info = await client.run(run_id).get()
        status = info.get("status") if isinstance(info, dict) else getattr(info, "status", None)
        status = getattr(status, "value", status)
        return str(status).upper() if status else None

    return get_status


async def run(actor=Actor) -> None:
    """The actor logic; ``actor`` is the (initialised) Apify ``Actor``.

    Incremental runs first take the best-effort ``stateKey`` lock (:class:`incremental.RunLock`)
    and release it when done, whatever happens."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    inp = await actor.get_input() or {}
    lock: incremental.RunLock | None = None
    if inp.get("onlyNewJobs"):
        store = await actor.open_key_value_store(name=incremental.STORE_NAME)
        lock = incremental.RunLock(store, inp.get("stateKey"), os.environ.get(ENV_RUN_ID, "").strip(),
                                   _run_status_getter(actor), actor.log)
        busy = await lock.acquire()
        if busy:
            await _fail(actor, busy)
            return
    try:
        await _run(actor, inp)
    finally:
        if lock is not None:
            with contextlib.suppress(Exception):
                await lock.release()


async def _run(actor, inp: dict) -> None:
    log = actor.log
    charge = charging_enabled()
    log.info("Pay-per-event charging %s (%s=%s)", "ENABLED" if charge else "disabled",
             ENV_CHARGING, os.environ.get(ENV_CHARGING, "unset"))

    cm = actor.get_charging_manager()
    pricing = cm.get_pricing_info()
    ppe = bool(pricing.is_pay_per_event)
    on = enabled_synthetic_events(pricing.per_event_prices) if ppe else []
    if on and charge:
        msg = (f"The pay-per-event pricing has a price on {', '.join(on)}. The platform would bill it for every "
               f"dataset item on top of the {JOB_EVENT!r} event (double charge). Remove that event in the "
               "Actor's monetization settings; refusing to run.")
        await _fail(actor, msg)
        return
    if on:
        log.warning("Built-in pricing event(s) %s look ENABLED. They are charged by the platform regardless of "
                    "%s and would double-charge with %s if charging were on. Turn them OFF in the Console "
                    "pricing for this Actor.", ", ".join(on), ENV_CHARGING, JOB_EVENT)
    if charge and not ppe:
        log.warning("%s=1 but this run does not use pay-per-event pricing: nothing is charged and no "
                    "budget limit is enforced.", ENV_CHARGING)
    if charge and ppe and not (pricing.per_event_prices or {}).get(JOB_EVENT):
        msg = (f"{ENV_CHARGING}=1 but pay-per-event pricing has no price for {JOB_EVENT!r}; "
               "refusing to deliver unbilled jobs.")
        await _fail(actor, msg)
        return
    metered = charge and ppe  # budget enforcement + charge-aware pushes

    # ---- input -----------------------------------------------------------------
    refs: list[BoardRef] = []
    bad: list[dict] = []
    for entry in _entries(inp.get("boards")):
        try:
            ref = parse_board(entry)
        except ValueError as exc:
            log.error("Skipping %r: %s", entry, exc)
            bad.append({"input": entry, "error": str(exc)})
            continue
        if ref not in refs:
            refs.append(ref)
    if not refs:
        await _fail(actor, "No valid Greenhouse boards in input.", invalidInputs=bad)
        return

    try:
        updated_since = parse_updated_since(inp.get("updatedSince"))
    except ValueError as exc:
        await _fail(actor, str(exc), invalidInputs=bad)
        return
    date_field = inp.get("updatedSinceField") or "updatedAt"
    if date_field not in DATE_FIELDS:
        date_field = "updatedAt"
    content = (inp.get("includeContent") or "text").strip().lower()
    if content not in CONTENT_MODES:
        content = "text"
    only_new = bool(inp.get("onlyNewJobs", False))
    flt = JobFilter(
        keyword=(inp.get("keyword") or "").strip() or None,
        location=(inp.get("location") or "").strip() or None,
        remote_only=bool(inp.get("remoteOnly", False)),
        departments=_names(inp.get("departments")),
        offices=_names(inp.get("offices")),
        updated_since=updated_since,
        date_field=date_field,
    )
    opts = Options(
        include_content=content,
        include_pay_ranges=bool(inp.get("includePayRanges", False)),
        include_questions=bool(inp.get("includeQuestions", False)),
        include_custom_fields=bool(inp.get("includeCustomFields", True)),
        only_new=only_new,
    )
    max_items = max(1, int(inp.get("maxItems") or DEFAULT_MAX_ITEMS))

    # ---- restart-safe state --------------------------------------------------
    state = await actor.use_state(_new_state(), key=STATE_KEY)
    for k, v in _new_state().items():
        state.setdefault(k, v)
    seen: set[str] = set(state["seen"])
    resumed = bool(state["pushed"])
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
        log.info("Resuming: %d jobs already delivered", state["pushed"])

    # ---- incremental state (named store, shared between runs) -----------------
    inc: incremental.IncrementalState | None = None
    known: set[str] = set()
    if only_new:
        store = await actor.open_key_value_store(name=incremental.STORE_NAME)
        inc = incremental.IncrementalState(store, inp.get("stateKey"))
        await inc.load()
        known = inc.keys()
        inc.mark(_confirmed(state))  # delivered (and charged) before a restart of this run
        seen |= known
        log.info("Incremental mode, stateKey=%r: %d jobs already seen (%d pruned after %d days)%s.",
                 inc.state_key, inc.loaded, inc.pruned, incremental.RETENTION_DAYS,
                 "; first run, emitting everything" if inc.first_run else "")

    stopped = "completed"
    budget_capped = False
    this_run = 0
    remaining = max_items - state["pushed"]
    if remaining <= 0:
        stopped = "maxItems"

    # ---- remaining budget for the job event, before any fetching ---------------
    if metered and remaining > 0:
        capacity = cm.compute_push_data_limit(remaining, JOB_EVENT, is_default_dataset=True)
        if capacity <= 0:
            log.warning("No budget left for %r (max total charge reached); not fetching.", JOB_EVENT)
            stopped = "budget"
            remaining = 0
        elif capacity < remaining:
            log.info("Budget allows only %d more jobs (requested %d).", capacity, remaining)
            remaining = capacity
            budget_capped = True

    report = RunReport()
    errors: dict[str, str] = state["errors"]

    def on_error(ref, exc) -> None:
        errors[ref.key] = f"{type(exc).__name__}: {exc}"

    def sync_notes() -> None:
        # rebuilt from base_notes each time, so repeated calls never double count
        state["notes"] = _merge_notes(base_notes, report)

    report.on_change = sync_notes  # the autosaved state follows every report change

    async def checkpoint() -> None:
        sync_notes()
        if inc is not None:
            await inc.save()
        await actor.set_status_message(f"{state['pushed']} jobs delivered")
        await _persist(actor)

    async def push_metered(job):
        # persisted before the write: if the run dies after the dataset write but before the
        # charge is confirmed, the next start finds this key and lists the row in
        # SUMMARY.unbilledJobs (possibly unbilled). A failed push/charge is never re-sent,
        # in this process or after a restart: its outcome is ambiguous (the platform may have
        # accepted it, or still be executing it), so it propagates and the run fails.
        await actor.set_value(PENDING_KEY, {"pending": job.key})
        return await actor.push_data(job.to_dict(), charged_event_name=JOB_EVENT)

    outcomes: dict[str, str] = {}
    # jobs each board already delivered before a restart count toward its fair share
    prior = [state["perBoard"].get(ref.token, 0) for ref in refs]

    if remaining > 0:
        try:
            async with RateLimitedClient() as client:
                # aclosing: leaving the loop (budget/maxItems) must cancel and await the
                # worker tasks *before* the client they use is closed
                async with contextlib.aclosing(iter_jobs(
                    client, refs, flt, opts,
                    max_items=remaining,
                    on_error=on_error,
                    seen=seen,
                    known=known,
                    touch=inc.touch if inc else None,
                    report=report,
                    prior=prior,
                    outcomes=outcomes,
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
                        state["seen"].append(job.key)
                        if inc is not None:
                            inc.mark([job.key])   # only after the push (and charge) went through
                        state["pushed"] += 1
                        this_run += 1
                        state["perBoard"][job.board_token] = state["perBoard"].get(job.board_token, 0) + 1
                        if metered and res.event_charge_limit_reached:
                            log.warning("Max total charge reached; stopping.")
                            stopped = "budget"
                            break
                        if state["pushed"] % PERSIST_EVERY == 0:
                            await checkpoint()
        finally:  # an unexpected failure must not forget jobs that were already delivered
            if inc is not None:
                with contextlib.suppress(Exception):
                    await inc.save()

        for ref in refs:  # a board that now ran to the end no longer counts as failed
            if outcomes.get(ref.key) == COMPLETED:
                errors.pop(ref.key, None)
        if stopped == "completed" and state["pushed"] >= max_items:
            stopped = "maxItems"
        elif stopped == "completed" and budget_capped and this_run >= remaining:
            stopped = "budget"

    sync_notes()
    notes = state["notes"]
    if metered:
        await actor.set_value(PENDING_KEY, {"pending": None})
    if inc is not None:
        await inc.save()
    for token, why in notes.get("hierarchyUnavailable", {}).items():
        log.warning("%s: department/office hierarchy unavailable (%s); paths and hierarchy filters are limited.",
                    token, why)
    for token, miss in notes.get("unmatchedFilters", {}).items():
        log.warning("%s: no department/office named %s on this board (see %s).", token,
                    ", ".join(repr(x) for x in miss.get("departments", []) + miss.get("offices", [])), TREES_KEY)
    summary = {
        "jobsPushed": state["pushed"],
        "jobsPushedThisRun": this_run,
        "resumed": resumed,
        "stoppedReason": stopped,
        "boards": per_board_summary(refs, state["perBoard"], outcomes, errors, notes, stopped),
        "errors": dict(errors),
        "invalidInputs": bad,
        "unmatchedFilters": notes.get("unmatchedFilters", {}),
        "hierarchyUnavailable": notes.get("hierarchyUnavailable", {}),
        "detailFailures": notes.get("detailFailures", {}),
        "incremental": None if inc is None else {
            "enabled": True,
            "stateKey": inc.state_key,
            "storeName": incremental.STORE_NAME,
            "firstRun": inc.first_run,
            "newJobsDelivered": this_run,
            "alreadySeenSkipped": sum(notes.get("alreadySeen", {}).values()),
            "trackedJobs": len(inc.seen),
            "prunedOlderThanDays": incremental.RETENTION_DAYS,
            "pruned": inc.pruned,
            "stateEvicted": inc.evicted,
            "maxTrackedJobs": inc.max_entries,
        },
        "chargingEnabled": charge,
        "unbilledJobs": notes.get("unbilledJobs", {}),
    }
    if report.trees:  # department/office trees of the boards read in this process, merged over earlier ones
        trees = await actor.get_value(TREES_KEY) or {}
        trees.update(report.trees)
        await actor.set_value(TREES_KEY, trees)
    await _persist(actor)
    await actor.set_value("SUMMARY", summary)
    log.info("Done: %s", {k: v for k, v in summary.items() if k != "boards"})
    msg = f"Delivered {state['pushed']} jobs from {sum(1 for v in state['perBoard'].values() if v)} boards"
    if stopped == "budget":
        msg += "; stopped at max total charge"
    if errors:
        msg += f"; {len(errors)} board errors"
    await actor.set_status_message(msg)


async def main() -> None:
    async with Actor:
        await run(Actor)
