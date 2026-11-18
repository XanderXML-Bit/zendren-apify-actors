"""Incremental ("only new jobs") state: which jobs earlier runs already delivered.

Stored in a *named* key-value store so it survives between runs (and is shared by every
run that uses the same ``stateKey``). One record per ``stateKey``::

    {"version": 1, "seen": {"<board token>|<job id>": "YYYY-MM-DD", ...}}

The date is when the job was last delivered or last seen on its board; entries not
refreshed for ``RETENTION_DAYS`` are pruned, so a job that vanished for 90 days and comes
back is delivered again. The record is also capped at ``MAX_ENTRIES``: beyond that the
least recently seen entries are evicted (an evicted job that is still listed is delivered
again). Overlapping runs with the same ``stateKey`` are not supported; :class:`RunLock` is a
best-effort guard against them. No Apify imports: the caller passes the store.
"""

from __future__ import annotations

import hashlib
import heapq
import re
from datetime import date, datetime, timedelta, timezone

STORE_NAME = "greenhouse-job-feed-state"
RETENTION_DAYS = 90
MAX_ENTRIES = 200_000
DEFAULT_STATE_KEY = "default"
LOCK_PREFIX = "LOCK-"
# Run statuses in which a run can still write: its lock is respected.
LIVE_RUN_STATUSES = ("READY", "RUNNING", "TIMING-OUT", "ABORTING")

_RECORD_PREFIX = "STATE-"
_DIGEST_CHARS = 12
_MAX_KEY_LEN = 200   # the platform allows 256; the lock record adds 5 more


def normalize_state_key(state_key: str | None) -> str:
    """The identity of a state: the supplied key without surrounding whitespace; empty means ``default``.
    Otherwise it is kept exactly (case, punctuation and unicode all distinguish keys)."""
    return (state_key or "").strip() or DEFAULT_STATE_KEY


def record_key(state_key: str | None) -> str:
    """KV-store record key: ``STATE-<readable prefix>-<12 hex of sha256(original key)>``.

    The prefix is the key with every character the store does not accept replaced by ``_`` and
    shortened so the whole record key stays within ``_MAX_KEY_LEN``; it is only there for humans.
    The digest of the *original* key (not of the sanitized prefix) is always appended, so
    ``daily/a``, ``daily?a`` and ``daily_a`` or two keys with the same long prefix never share a record."""
    original = normalize_state_key(state_key)
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    room = _MAX_KEY_LEN - len(_RECORD_PREFIX) - 1 - _DIGEST_CHARS
    prefix = re.sub(r"[^A-Za-z0-9!\-_.'()]", "_", original)[:room]
    return f"{_RECORD_PREFIX}{prefix}-{digest}"


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _evict(seen: dict[str, str], limit: int) -> int:
    """Drop the least recently seen entries beyond ``limit`` (ties: smaller key first). Returns the count."""
    excess = len(seen) - limit
    if excess <= 0:
        return 0
    for _, key in heapq.nsmallest(excess, ((day, key) for key, day in seen.items())):
        del seen[key]
    return excess


class IncrementalState:
    def __init__(self, store, state_key: str | None, today: date | None = None,
                 max_entries: int | None = None) -> None:
        self.store = store
        self.state_key = normalize_state_key(state_key)
        self.record = record_key(self.state_key)
        self.today = today or today_utc()
        self.max_entries = max_entries or MAX_ENTRIES
        self.seen: dict[str, str] = {}
        self.pruned = 0
        self.evicted = 0
        self.loaded = 0
        self.first_run = True

    async def load(self) -> None:
        data = await self.store.get_value(self.record)
        raw = data.get("seen") if isinstance(data, dict) else None
        cutoff = self.today - timedelta(days=RETENTION_DAYS)
        self.first_run = not raw
        for key, day in (raw or {}).items():
            try:
                when = date.fromisoformat(day)
            except (TypeError, ValueError):
                when = self.today
            if when < cutoff:
                self.pruned += 1
            else:
                self.seen[key] = when.isoformat()
        self.evicted += _evict(self.seen, self.max_entries)   # after age pruning
        self.loaded = len(self.seen)

    def keys(self) -> set[str]:
        return set(self.seen)

    def touch(self, key: str) -> None:
        """A tracked job is still listed on its board: refresh its date."""
        if key in self.seen:
            self.seen[key] = self.today.isoformat()

    def mark(self, keys) -> None:
        for key in keys:
            self.seen[key] = self.today.isoformat()

    async def save(self) -> None:
        self.evicted += _evict(self.seen, self.max_entries)
        await self.store.set_value(self.record, {"version": 1, "seen": dict(self.seen)})


class RunLock:
    """Best-effort guard against two runs using one ``stateKey`` at the same time.

    ``LOCK-<record key>`` in the same named store holds ``{"runId", "at"}``. At the start of a run
    whose lock belongs to a *different* run, that run's status is looked up (``get_run_status``,
    returns the platform status string or ``None`` when the run is unknown); if it can still write
    (``LIVE_RUN_STATUSES``) the new run must stop. This is not an atomic lock: two runs starting
    in the same instant can both pass. Without a run id (local runs) nothing is checked or stored."""

    def __init__(self, store, state_key: str | None, run_id: str | None, get_run_status=None, log=None) -> None:
        self.store = store
        self.key = LOCK_PREFIX + record_key(state_key)
        self.run_id = run_id or None
        self.get_run_status = get_run_status
        self.log = log
        self.held = False

    async def acquire(self) -> str | None:
        """Take the lock. Returns an error message when another live run holds it, else ``None``."""
        if not self.run_id:
            return None
        current = await self.store.get_value(self.key)
        other = current.get("runId") if isinstance(current, dict) else None
        if other and other != self.run_id and self.get_run_status is not None:
            try:
                status = await self.get_run_status(other)
            except Exception as exc:  # best effort: an unreadable status never blocks a run
                if self.log:
                    self.log.warning("Could not check run %s holding the stateKey lock (%s); continuing.", other, exc)
                status = None
            if status in LIVE_RUN_STATUSES:
                return (f"another run is using this stateKey (run {other}, status {status}, since "
                        f"{current.get('at')}). Wait for it to finish or use a different stateKey; "
                        "overlapping runs with the same stateKey are not supported.")
        await self.store.set_value(self.key, {"runId": self.run_id, "at": datetime.now(timezone.utc).isoformat()})
        self.held = True
        return None

    async def release(self) -> None:
        if not self.held:
            return
        self.held = False
        current = await self.store.get_value(self.key)
        if isinstance(current, dict) and current.get("runId") == self.run_id:
            await self.store.set_value(self.key, None)   # None deletes the record
