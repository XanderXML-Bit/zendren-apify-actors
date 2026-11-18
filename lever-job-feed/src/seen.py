"""Incremental mode: posting keys seen by earlier runs, in a named key-value store.

The state record is ``STATE-<readable prefix>-<12 hex of sha256(stateKey)>``. It is capped at
:data:`MAX_ENTRIES` keys (least recently seen are evicted first) and pruned after 90 days.
A best-effort ``LOCK-<record key>`` record keeps two overlapping runs from sharing one state key.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger("leverfeed.seen")

STORE_NAME = "lever-job-feed-state"
PRUNE_AFTER_DAYS = 90
MAX_ENTRIES = 200_000  # roughly 15 MB of JSON; keeps load/save memory far below the 1024 MB ceiling
STATE_VERSION = 1

RECORD_PREFIX = "STATE-"
LOCK_PREFIX = "LOCK-"
MAX_KEY_LENGTH = 256  # Apify's limit for a key-value record key
# the record key must leave room for the lock key that is derived from it
MAX_RECORD_KEY = MAX_KEY_LENGTH - len(LOCK_PREFIX)
_BAD_KEY_CHARS = re.compile(r"[^A-Za-z0-9!\-_.'()]")


def today_days(now: datetime | None = None) -> int:
    return int((now or datetime.now(timezone.utc)).timestamp() // 86400)


DEFAULT_STATE_KEY = "default"


def normalize_state_key(state_key: str | None) -> str:
    """The key exactly as typed; only an omitted or blank key becomes ``default``. The hash and the
    lock are derived from this value, so ``daily`` and `` daily `` are two different states."""
    if not isinstance(state_key, str) or not state_key.strip():
        return DEFAULT_STATE_KEY
    return state_key


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def record_key(state_key: str | None) -> str:
    """Key-value record key for a user's ``stateKey``: always ``STATE-<prefix>-<hash>``.

    The prefix is the trimmed key with every character the store does not allow replaced by ``_``; the
    hash is of the original key, so two different state keys never share a record, whether or not
    they sanitize to the same prefix (``daily/a``, ``daily?a`` and ``daily_a`` all differ). The
    suffix is reserved before the prefix is shortened, so long keys keep their hash."""
    original = normalize_state_key(state_key)
    suffix = "-" + _short_hash(original)
    budget = MAX_RECORD_KEY - len(RECORD_PREFIX) - len(suffix)
    readable = original.strip()  # cosmetic only: the hash above is of the original text
    return RECORD_PREFIX + _BAD_KEY_CHARS.sub("_", readable)[:budget] + suffix


def lock_key(state_key: str | None) -> str:
    return LOCK_PREFIX + record_key(state_key)


def posting_key(site: str, posting_id: str) -> str:
    """Identity of a posting: the site and Lever's posting id, nothing that depends on input options
    (region, description format, lists)."""
    return f"{site}|{posting_id}"


class SeenStore:
    """``{site|postingId: day last seen on a board}``.

    A key is added only after its posting was pushed and its charge confirmed. Keys still present on
    a board that was read completely get their day refreshed, so a long-running posting is never
    forgotten; keys not seen for 90 days are dropped. Above :data:`MAX_ENTRIES` the least recently
    seen keys are evicted (on load and on every save); ``evicted`` counts them. An evicted posting
    that is still on a board is delivered, and charged, again."""

    def __init__(self, kvs, state_key: str, today: int | None = None, max_entries: int | None = None) -> None:
        self.kvs = kvs
        self.key = record_key(state_key)
        self.today = today_days() if today is None else today
        self.max_entries = MAX_ENTRIES if max_entries is None else max_entries
        self.seen: dict[str, int] = {}
        self.first_run = True
        self.pruned = 0
        self.evicted = 0

    async def load(self) -> None:
        data = await self.kvs.get_value(self.key)
        if isinstance(data, dict) and isinstance(data.get("seen"), dict):
            self.seen = {k: int(v) for k, v in data["seen"].items() if isinstance(v, (int, float))}
            self.first_run = False
            if len(self.seen) > self.max_entries:
                # age pruning normally waits for save(), after this run refreshed the postings that are
                # still on a board; it only happens early when the state is over the cap, so that
                # expired keys are dropped before any still-valid key is evicted
                self.prune()
                self._evict_over_cap()  # before anything is delivered, so the run never outgrows the cap

    def __contains__(self, key: str) -> bool:
        return key in self.seen

    def mark(self, keys) -> None:
        for k in keys:
            self.seen[k] = self.today

    def refresh(self, keys) -> None:
        for k in keys:
            if k in self.seen:
                self.seen[k] = self.today

    def prune(self) -> int:
        cutoff = self.today - PRUNE_AFTER_DAYS
        old = [k for k, d in self.seen.items() if d < cutoff]
        for k in old:
            del self.seen[k]
        self.pruned += len(old)
        return len(old)

    def _evict_over_cap(self) -> int:
        """Keep at most ``max_entries``: oldest last-seen day goes first; ties go in the order the
        keys were stored (stable sort)."""
        extra = len(self.seen) - self.max_entries
        if extra <= 0:
            return 0
        for key in sorted(self.seen, key=self.seen.__getitem__)[:extra]:
            del self.seen[key]
        self.evicted += extra
        log.warning("State %r holds more than %d postings: evicted the %d least recently seen; if they "
                    "are still on a board they will be delivered again.", self.key, self.max_entries, extra)
        return extra

    async def save(self) -> None:
        self.prune()
        self._evict_over_cap()
        await self.kvs.set_value(self.key, {
            "version": STATE_VERSION,
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "seen": self.seen,
        })


# ---------------------------------------------------------------- best-effort run lock
class StateLockedError(Exception):
    """Another run is still working with the same state key."""


def _run_status(run) -> str | None:
    status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
    status = getattr(status, "value", status)
    return str(status).upper() if status else None


class StateLock:
    """Best-effort lock so two overlapping runs do not share one ``stateKey``.

    ``LOCK-<record key>`` in the same named store holds ``{runId, at}``. At start, a lock held by
    another run that the platform still reports as RUNNING or READY stops this run; any other lock
    (finished run, unknown run, crashed run) is taken over. The lock is released at the end of the
    run. It is not atomic (two runs starting in the same instant can both pass), and it is skipped
    when ``ACTOR_RUN_ID`` is not set (local runs). A migrated or restarted run keeps its run id and
    so takes back its own lock."""

    def __init__(self, kvs, state_key: str, run_id: str | None, client=None) -> None:
        self.kvs = kvs
        self.key = lock_key(state_key)
        self.run_id = run_id or None
        self.client = client
        self.held = False

    async def _other_run_active(self, holder: str) -> bool:
        if self.client is None:
            return False
        try:
            run = await self.client.run(holder).get()
        except Exception as exc:  # noqa: BLE001 - best effort: an unreadable run must not block this one
            log.warning("Could not check run %s that holds the state lock (%s: %s); taking the lock.",
                        holder, type(exc).__name__, exc)
            return False
        return _run_status(run) in ("RUNNING", "READY") if run else False

    async def acquire(self) -> None:
        if not self.run_id:
            return
        current = await self.kvs.get_value(self.key)
        holder = current.get("runId") if isinstance(current, dict) else None
        if holder and holder != self.run_id and await self._other_run_active(holder):
            raise StateLockedError(
                f"Run {holder} is still using this state key (since {current.get('at')}). Wait for it to "
                f"finish or use a different stateKey; two overlapping runs would deliver the same postings twice.")
        await self.kvs.set_value(self.key, {"runId": self.run_id,
                                            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")
                                            .replace("+00:00", "Z")})
        self.held = True
        # a run that wrote at the same moment wins or loses here: whoever is not on record stops
        check = await self.kvs.get_value(self.key)
        winner = check.get("runId") if isinstance(check, dict) else None
        if winner and winner != self.run_id and await self._other_run_active(winner):
            self.held = False
            raise StateLockedError(f"Run {winner} took the state key at the same moment; this run stops.")

    async def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            current = await self.kvs.get_value(self.key)
            if isinstance(current, dict) and current.get("runId") == self.run_id:
                await self.kvs.delete_value(self.key)
        except Exception as exc:  # noqa: BLE001 - a stale lock is harmless (it is taken over)
            log.warning("Could not release the state lock: %s: %s", type(exc).__name__, exc)
