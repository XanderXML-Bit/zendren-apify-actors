"""Apify entrypoint: Lever job feed.

Charging: the platform charges the synthetic ``apify-actor-start`` event itself. This code charges
one custom event, ``job``, per record pushed, and only when the environment variable
``CHARGING_ENABLED=1``. The synthetic ``apify-default-dataset-item`` event must stay OFF.

With charging on, the run never fetches or pushes beyond the user's max total charge: the remaining
capacity for ``job`` is checked before any work and every record goes through the charge-aware
``push_data(..., charged_event_name=...)``.

Restarts (platform migration): the keys of delivered records and the pushed count live in the run's
key-value store (``use_state``) and are rebuilt from the dataset if needed, so a restarted run skips
delivered records, never charges them twice and honours ``maxItems`` across restarts.

``push_data(..., charged_event_name=...)`` is Apify's own write-and-charge call; it is not atomic. If a
run dies between the dataset write and the charge, the record is delivered but may not have been
billed. Such a record is never charged again (an ambiguous charge is not re-sent); it is listed in
``SUMMARY.unbilledJobs`` and is kept out of the incremental state, so a later ``onlyNewJobs`` run
delivers (and bills) it properly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from apify import Actor

from .http import LeverError, RateLimitedClient
from .lever import (
    DESCRIPTION_FORMATS, ClientFilters, ServerFilters, discover, fetch_site, normalize,
    normalize_workplace_types, parse_posted_since,
)
from .plan import fair_split
from .seen import (
    STORE_NAME, SeenStore, StateLock, StateLockedError, normalize_state_key, posting_key,
)
from .sites import REGIONS, SiteRef, parse_site, split_entries

DEFAULT_MAX_ITEMS = 100
FETCH_CONCURRENCY = 4
ENV_CHARGING = "CHARGING_ENABLED"
JOB_EVENT = "job"
DATASET_ITEM_EVENT = "apify-default-dataset-item"
ENV_RUN_ID = "ACTOR_RUN_ID"
STATE_KEY = "LEVERFEED_STATE"
# Written *before* every charged push: {"pending": <record key or None>}. See _note_pending_unbilled.
PENDING_KEY = "LEVERFEED_PENDING"
PERSIST_EVERY = 25
DISCOVER_ID = "@filters"

# SUMMARY.sites[<site>].reason
R_COMPLETED = "completed"    # everything that matched was delivered
R_NO_MATCHES = "no_matches"  # the site was read, nothing to deliver (no match, or nothing new)
R_CAP = "cap_reached"        # maxItems (or the site's fair share of it) stopped delivery
R_ERROR = "error"            # the site could not be read
R_BUDGET = "budget"          # the max total charge stopped delivery

ZERO_HINT = ("No postings matched the server-side filters. Lever matches teams, departments, commitments "
             "and levels case-insensitively but the whole value, and locations case-sensitively; run with "
             "discoverFilters to list the exact values of this site.")


def charging_enabled() -> bool:
    return os.environ.get(ENV_CHARGING, "0").strip() == "1"


def _event_price(pricing, event: str) -> Decimal:
    try:
        return Decimal(str((pricing.per_event_prices or {}).get(event) or 0))
    except InvalidOperation:
        return Decimal(0)


def make_client() -> RateLimitedClient:
    return RateLimitedClient()


# ---------------------------------------------------------------- fetch phase
@dataclass
class SiteResult:
    ref: SiteRef
    region: str | None = None
    error: str | None = None
    fetched: int = 0         # postings on the board after the server-side filters
    matched: int = 0         # ... that also pass the client-side filters
    already_seen: int = 0    # matched, but delivered by an earlier run (incremental mode)
    candidates: list = field(default_factory=list)  # (record key, raw) newest first, undelivered
    more: bool = False       # further undelivered postings beyond ``candidates`` (kept to ``keep``)
    record: dict | None = None  # discover mode: the filters record

    @property
    def name(self) -> str:
        return self.ref.site


async def fetch_one(client, ref: SiteRef, *, region_setting: str, server: ServerFilters, client_filters: ClientFilters,
                    discover_mode: bool, scraped_at: str, delivered: set[str], seen: SeenStore | None,
                    keep: int) -> SiteResult:
    res = SiteResult(ref)
    try:
        region, raw = await fetch_site(client, ref, region_setting, server)
    except LeverError as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        return res
    except Exception as exc:  # noqa: BLE001 - one site must never take the others down
        res.error = f"{type(exc).__name__}: {exc}"
        return res
    res.region, res.fetched = region, len(raw)
    if discover_mode:
        res.record = discover(raw, ref.site, region, scraped_at)
        key = posting_key(ref.site, DISCOVER_ID)
        res.candidates = [] if key in delivered else [(key, res.record)]
        res.matched = 1
        return res
    if seen is not None:
        seen.refresh(posting_key(ref.site, str(p["id"])) for p in raw)  # board fully read: keep them fresh
    matched = [p for p in raw if client_filters.match(p)]
    res.matched = len(matched)
    matched.sort(key=lambda p: p.get("createdAt") if isinstance(p.get("createdAt"), (int, float)) else 0,
                 reverse=True)
    cands = []
    for p in matched:
        key = posting_key(ref.site, str(p["id"]))
        if key in delivered:
            continue
        if seen is not None and key in seen:
            res.already_seen += 1
            continue
        cands.append((key, p))
    res.candidates = cands[:max(keep, 0)]
    res.more = len(cands) > len(res.candidates)
    return res


async def fetch_all(client, refs, **kw) -> list[SiteResult]:
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(ref):
        async with sem:
            return await fetch_one(client, ref, **kw)

    return list(await asyncio.gather(*(one(r) for r in refs)))


# ---------------------------------------------------------------- state helpers
def _new_state() -> dict:
    return {"seen": [], "pushed": 0, "perSite": {}, "failed": {}, "notes": {}}


async def _persist(actor) -> None:
    kvs = await actor.open_key_value_store()
    await kvs.persist_autosaved_values()


def _row_key(item: dict) -> str:
    return f"{item.get('site')}|{item.get('postingId') or DISCOVER_ID}"


async def _scan_dataset(actor) -> tuple[list[str], dict[str, int]]:
    """Record keys (dataset order) and per-site counts of everything delivered so far."""
    dataset = await actor.open_dataset()
    keys: list[str] = []
    per_site: dict[str, int] = {}
    async for item in dataset.iterate_items(fields=["site", "postingId", "recordType"]):
        keys.append(_row_key(item))
        per_site[item.get("site")] = per_site.get(item.get("site"), 0) + 1
    return keys, per_site


async def _recover_from_dataset(actor, state: dict, seen: set[str], *, force: bool = False) -> list[str] | None:
    """State is persisted periodically, so a crash can leave records in the dataset that the state
    does not know. Rebuild their keys, the pushed count and the per-site counts from the dataset."""
    dataset = await actor.open_dataset()
    info = await dataset.get_metadata()
    if not force and (info.item_count or 0) <= state["pushed"]:
        return None
    if (info.item_count or 0) > state["pushed"]:
        actor.log.warning("Dataset has %d items but saved state only %d; rebuilding from the dataset.",
                          info.item_count, state["pushed"])
    keys, per_site = await _scan_dataset(actor)
    for key in keys:
        if key not in seen:
            seen.add(key)
            state["seen"].append(key)
    state["pushed"] = max(state["pushed"], len(keys))
    if per_site:
        state["perSite"] = per_site  # the dataset is the source of truth
    return keys


async def _note_pending_unbilled(actor, cm, state: dict, keys: list[str] | None = None) -> None:
    """Restart only: a pending record found in the dataset is delivered, but its charge is ambiguous.

    ``push_data(..., charged_event_name=...)`` writes the dataset first and charges second, and
    before each push we persist its key (``PENDING_KEY``). If that key is in the dataset, the charge
    may or may not have been accepted. An ambiguous charge is NEVER re-sent: the record counts as
    delivered and is listed in ``SUMMARY.unbilledJobs`` as possibly unbilled. Only when the startup
    charged count already covers every record is it known to be paid."""
    marker = await actor.get_value(PENDING_KEY) or {}
    pending = marker.get("pending")
    if not pending:
        return
    if keys is None:
        keys, _ = await _scan_dataset(actor)
    if pending not in keys:  # crashed before the write: the record is simply re-delivered
        return
    if cm.get_charged_event_count(JOB_EVENT) >= len(keys):
        return  # the charge is confirmed
    actor.log.warning("Record %s is in the dataset but its charge is unconfirmed; it is NOT charged again "
                      "(possibly unbilled).", pending)
    state["notes"].setdefault("unbilledJobs", {})[pending] = "delivered, charge outcome unconfirmed (possibly unbilled)"


# ---------------------------------------------------------------- run
async def _fail(actor, msg: str, errors: list | None = None) -> None:
    """Fail the run before any record was delivered, still writing a SUMMARY (the README promises one per run)."""
    actor.log.error(msg)
    await actor.set_value("SUMMARY", {"recordsPushed": 0, "error": msg, "sites": {}, "errors": errors or []})
    await actor.fail(status_message=msg)


async def run(actor=Actor) -> None:
    """The actor logic; ``actor`` is the (initialised) Apify ``Actor``."""
    holder: dict = {}
    try:
        await _run(actor, holder)
    finally:
        lock = holder.get("lock")
        if lock is not None:
            await lock.release()


async def _run(actor, holder: dict) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = actor.log
    inp = await actor.get_input() or {}
    charge = charging_enabled()
    log.info("Pay-per-event charging %s (%s=%s)", "ENABLED" if charge else "disabled",
             ENV_CHARGING, os.environ.get(ENV_CHARGING, "unset"))

    cm = actor.get_charging_manager()
    pricing = cm.get_pricing_info()
    ppe = bool(pricing.is_pay_per_event)
    item_price = _event_price(pricing, DATASET_ITEM_EVENT) if ppe else Decimal(0)
    if item_price > 0:
        # the platform charges this event for every default-dataset write on its own, whether or not
        # CHARGING_ENABLED is set, so with a price on it every record would be billed twice
        await _fail(actor, f"The built-in {DATASET_ITEM_EVENT!r} event has a price ({item_price}) while "
                           f"{JOB_EVENT!r} is charged per record: every record would be billed twice. "
                           f"Turn the {DATASET_ITEM_EVENT!r} event OFF in the Console pricing. Nothing was fetched.")
        return
    if charge and not ppe:
        log.warning("%s=1 but this run does not use pay-per-event pricing: nothing is charged and no "
                    "budget limit is enforced.", ENV_CHARGING)
    if charge and ppe and not (pricing.per_event_prices or {}).get(JOB_EVENT):
        # the SDK would deliver items and "charge" them at zero: refuse instead
        await _fail(actor, f"{ENV_CHARGING}=1 but pay-per-event pricing has no price for {JOB_EVENT!r}; "
                           f"refusing to deliver unbilled records.")
        return
    metered = charge and ppe  # budget enforcement + charge-aware pushes

    # ---- input ----------------------------------------------------------------
    refs: list[SiteRef] = []
    errors: list[dict] = []
    for entry in split_entries(inp.get("sites")):
        try:
            ref = parse_site(entry)
        except ValueError as exc:
            log.error("Skipping %r: %s", entry, exc)
            errors.append({"input": entry, "error": str(exc)})
            continue
        if all(r.site != ref.site for r in refs):
            refs.append(ref)
    if not refs:
        await _fail(actor, "No valid Lever sites in input.", errors)
        return
    region_setting = (inp.get("region") or "auto").strip().lower()
    description_format = (inp.get("descriptionFormat") or "text").strip().lower()
    if region_setting not in REGIONS:
        await _fail(actor, f"Invalid region {region_setting!r}: use auto, global or eu.")
        return
    if description_format not in DESCRIPTION_FORMATS:
        await _fail(actor, f"Invalid descriptionFormat {description_format!r}: use none, text, html or markdown.")
        return
    try:
        client_filters = ClientFilters(
            keyword=(inp.get("keyword") or "").strip() or None,
            workplace_types=normalize_workplace_types(inp.get("workplaceTypes")),
            posted_since=parse_posted_since(inp.get("postedSince")),
            salary_only=bool(inp.get("salaryOnly", False)),
        )
    except ValueError as exc:
        await _fail(actor, str(exc))
        return
    server = ServerFilters.from_input(inp)
    include_lists = bool(inp.get("includeLists", True))
    discover_mode = bool(inp.get("discoverFilters", False))
    only_new = bool(inp.get("onlyNewJobs", False)) and not discover_mode
    try:
        max_items = int(inp.get("maxItems") or DEFAULT_MAX_ITEMS)
    except (TypeError, ValueError):
        await _fail(actor, "maxItems must be a whole number.")
        return
    max_items = max(1, max_items)
    if discover_mode:
        max_items = len(refs)  # one record per site; maxItems does not apply

    seen: SeenStore | None = None
    if only_new:
        state_key = normalize_state_key(inp.get("stateKey"))
        kvs = await actor.open_key_value_store(name=STORE_NAME)
        lock = StateLock(kvs, state_key, os.environ.get(ENV_RUN_ID), _api_client(actor))
        try:
            await lock.acquire()
        except StateLockedError as exc:
            await _fail(actor, str(exc))
            return
        holder["lock"] = lock
        seen = SeenStore(kvs, state_key)
        await seen.load()
        log.info("Incremental mode: %d postings remembered under %r (%s)", len(seen.seen), state_key,
                 "first run, everything is new" if seen.first_run else "later run")
        if seen.evicted:
            log.warning("%d remembered postings were evicted (state cap); they are delivered again if still listed.",
                        seen.evicted)

    # ---- restart-safe run state --------------------------------------------------
    state = await actor.use_state(_new_state(), key=STATE_KEY)
    for k, v in _new_state().items():
        state.setdefault(k, v)
    delivered: set[str] = set(state["seen"])
    resumed = bool(state["pushed"])
    marker = await actor.get_value(PENDING_KEY) if metered else None
    keys = await _recover_from_dataset(actor, state, delivered, force=bool(marker and marker.get("pending")))
    if metered:
        await _note_pending_unbilled(actor, cm, state, keys)
        if state["notes"].get("unbilledJobs"):
            await _persist(actor)  # the warning must be durable before its marker is cleared
        await actor.set_value(PENDING_KEY, {"pending": None})
    resumed = resumed or bool(state["pushed"])
    if resumed:
        log.info("Resuming: %d records already delivered", state["pushed"])

    stopped = "completed"
    budget_capped = False
    this_run = 0
    remaining = max_items - state["pushed"]
    if remaining <= 0:
        stopped = "maxItems"

    # ---- remaining budget for the job event, before any fetching ---------------------
    if metered and remaining > 0:
        capacity = cm.compute_push_data_limit(remaining, JOB_EVENT, is_default_dataset=True)
        if capacity <= 0:
            log.warning("No budget left for %r (max total charge reached); not fetching.", JOB_EVENT)
            stopped = "budget"
            remaining = 0
        elif capacity < remaining:
            log.info("Budget allows only %d more records (requested %d).", capacity, remaining)
            remaining = capacity
            budget_capped = True

    scraped_at = _now_iso()
    results: list[SiteResult] = []
    quotas: dict[str, int] = {}
    prior = {r.site: state["perSite"].get(r.site, 0) for r in refs}

    async def flush_seen() -> None:
        if seen is not None:
            # rows whose charge is unconfirmed (restart) never enter the named state: a later run delivers
            # (and bills) them again rather than skipping them as done
            unconfirmed = set(state["notes"].get("unbilledJobs") or {})
            seen.mark(k for k in state["seen"] if k not in unconfirmed)
            await seen.save()

    if remaining > 0:
        async with make_client() as client:
            results = await fetch_all(
                client, refs, region_setting=region_setting, server=server, client_filters=client_filters,
                discover_mode=discover_mode, scraped_at=scraped_at, delivered=delivered, seen=seen,
                keep=remaining)
        for res in results:
            if res.error:
                log.error("Site %r failed: %s", res.name, res.error)
                state["failed"][res.name] = res.error
            else:
                state["failed"].pop(res.name, None)
        ok = [r for r in results if not r.error]
        # prior deliveries count toward a site's share, so a restarted run stays fair
        alloc = fair_split([prior[r.name] + len(r.candidates) for r in ok], sum(prior[r.name] for r in ok) + remaining)
        quotas = {r.name: max(0, a - prior[r.name]) for r, a in zip(ok, alloc)}

        async def push_metered(rec_key: str, record: dict):
            # persisted before the write: if the run dies after the dataset write but before the
            # charge is confirmed, the next start finds this key and lists the record in
            # SUMMARY.unbilledJobs. A failed push/charge is never re-sent (its outcome is ambiguous).
            await actor.set_value(PENDING_KEY, {"pending": rec_key})
            return await actor.push_data(record, charged_event_name=JOB_EVENT)

        try:
            for res in ok:
                for rec_key, raw in res.candidates[:quotas[res.name]]:
                    if state["pushed"] >= max_items:
                        break
                    record = raw if discover_mode else normalize(raw, res.name, res.region, description_format,
                                                                 include_lists, scraped_at)
                    if metered:
                        pushed = await push_metered(rec_key, record)
                        if pushed.charged_count == 0:  # not pushed: nothing left to bill
                            log.warning("Max total charge reached; stopping.")
                            stopped = "budget"
                            break
                    else:
                        await actor.push_data(record)
                    state["seen"].append(rec_key)
                    delivered.add(rec_key)
                    state["pushed"] += 1
                    this_run += 1
                    state["perSite"][res.name] = state["perSite"].get(res.name, 0) + 1
                    if metered and pushed.event_charge_limit_reached:
                        log.warning("Max total charge reached; stopping.")
                        stopped = "budget"
                        break
                    if state["pushed"] % PERSIST_EVERY == 0:
                        await actor.set_status_message(f"{state['pushed']} records delivered")
                        await _persist(actor)
                        await flush_seen()
                if stopped == "budget":
                    break
        finally:
            # also on a failed push: whatever was delivered must not be re-sent by the next run
            await flush_seen()
        if stopped == "completed" and state["pushed"] >= max_items:
            stopped = "maxItems"
        elif stopped == "completed" and budget_capped and this_run >= remaining:
            stopped = "budget"
    else:
        # nothing left to fetch or push (maxItems already reached, or no budget): the rows recovered
        # from the dataset were delivered and charged by the earlier attempt, so the named history
        # must still learn them or the next onlyNewJobs run delivers and bills them again
        await flush_seen()

    if metered:
        await actor.set_value(PENDING_KEY, {"pending": None})

    # ---- summary --------------------------------------------------------------------
    by_name = {r.name: r for r in results}
    sites_summary: dict[str, dict] = {}
    for ref in refs:
        res = by_name.get(ref.site)
        count = state["perSite"].get(ref.site, 0)
        entry: dict = {"count": count}
        if res is None:  # never fetched: the run was already capped or out of budget at start
            entry["reason"] = R_BUDGET if stopped == "budget" else R_CAP
        elif res.error:
            entry["reason"] = R_ERROR
            entry["error"] = res.error
        else:
            sent_now = count - prior[res.name]
            if res.more or len(res.candidates) > sent_now:  # something that qualified was not delivered
                entry["reason"] = R_BUDGET if stopped == "budget" else R_CAP
            else:
                entry["reason"] = R_COMPLETED if count else R_NO_MATCHES
        if res is not None and not res.error:
            entry["region"] = res.region
            entry["postingsOnBoard"] = res.fetched
            if not discover_mode:
                entry["matched"] = res.matched
                if only_new:
                    entry["alreadySeen"] = res.already_seen
            if res.fetched == 0 and server.applied():
                entry["hint"] = ZERO_HINT
        sites_summary[ref.site] = entry
    summary = {
        "mode": "discoverFilters" if discover_mode else "jobs",
        "recordsPushed": state["pushed"],
        "recordsPushedThisRun": this_run,
        "resumed": resumed,
        "stoppedReason": stopped,
        "sites": sites_summary,
        "serverSideFilters": server.applied(),
        "clientSideFilters": {k: v for k, v in {
            "keyword": client_filters.keyword,
            "workplaceTypes": sorted(client_filters.workplace_types) or None,
            "postedSince": inp.get("postedSince") or None,
            "salaryOnly": client_filters.salary_only or None,
        }.items() if v},
        "region": region_setting,
        "errors": errors + [{"input": n, "error": e} for n, e in state["failed"].items()],
        "incremental": ({"enabled": True, "stateKey": normalize_state_key(inp.get("stateKey")), "store": STORE_NAME,
                         "firstRun": seen.first_run, "alreadySeenSkipped": sum(r.already_seen for r in results),
                         "newRecordsThisRun": this_run, "pruned": seen.pruned, "tracked": len(seen.seen)}
                        if seen is not None else {"enabled": False}),
        # incremental mode only: remembered postings dropped because the state exceeded its entry cap
        # (they are delivered again if still on a board); null otherwise
        "stateEvicted": seen.evicted if seen is not None else None,
        "chargingEnabled": charge,
        "unbilledJobs": state["notes"].get("unbilledJobs", {}),
    }
    await _persist(actor)
    await actor.set_value("SUMMARY", summary)
    log.info("Done: %s", summary)
    msg = f"Delivered {state['pushed']} {'filter records' if discover_mode else 'postings'} from {len(state['perSite'])} sites"
    if stopped == "budget":
        msg += "; stopped at max total charge"
    if summary["errors"]:
        msg += f"; {len(summary['errors'])} errors"
    await actor.set_status_message(msg)


def _api_client(actor):
    try:
        return actor.apify_client
    except Exception:  # noqa: BLE001 - not initialised (local tooling): the lock then only records itself
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def main() -> None:
    async with Actor:
        await run(Actor)
