"""State for "new jobs since last run" mode (no Apify imports; the store is passed in).

The state is one JSON record in a *named* key-value store (named stores outlive runs;
the default store of a run does not)::

    {"v": 1, "jobs": {"<ats>|<company slug>|<jobId>": <day last seen>, ...}}

``<day>`` is a whole number of days since 1970-01-01 (UTC), which keeps the record small.
Entries not seen for :data:`RETENTION_DAYS` are dropped when the state is loaded and saved.
The record is also capped at :data:`MAX_ENTRIES`: above that the least recently seen entries
are evicted (on load and on every save), counted in :attr:`SeenState.evicted` and reported as
``stateEvicted`` in the run SUMMARY. An evicted job that is still listed is delivered (and
charged) again, which is why the cap is generous.

A job is only ever added by :meth:`SeenState.mark` after it was actually pushed (and charged
when charging is on); :meth:`SeenState.touch` only refreshes jobs that are already in the state.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone

log = logging.getLogger("jobfeed")

STORE_NAME = "ats-job-feed-state"
RETENTION_DAYS = 90
MAX_ENTRIES = 200_000  # roughly 15 MB of JSON; keeps load/save memory far below the 1024 MB ceiling
FORMAT_VERSION = 1
RECORD_PREFIX = "STATE-"
MAX_RECORD_KEY = 256
_BAD_KEY_CHARS = re.compile(r"[^A-Za-z0-9!_.'()-]")


def today() -> int:
    """Current UTC day number (days since 1970-01-01)."""
    return int(datetime.now(timezone.utc).timestamp() // 86400)


def _short_hash(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def default_state_key(refs) -> str:
    """Stable key for a company list: the same companies in any order give the same key."""
    names = sorted({ref.key for ref in refs})
    return "companies-" + _short_hash("\n".join(names), 16)


def record_key(state_key: str) -> str:
    """Key-value record key for ``state_key``: only characters Apify accepts, at most 256.

    Always ``STATE-<readable sanitized prefix>-<hash of the original>``. The hash is of the
    original text, so two different state keys never share a record, whether or not they
    sanitize to the same prefix (and a valid-looking name can never equal another key's
    sanitized form plus its hash)."""
    suffix = "-" + _short_hash(state_key)
    budget = MAX_RECORD_KEY - len(RECORD_PREFIX) - len(suffix)
    return RECORD_PREFIX + _BAD_KEY_CHARS.sub("_", state_key)[:budget] + suffix


class SeenState:
    """The set of job identities delivered by earlier runs, with their last-seen day."""

    def __init__(self, kvs, key: str, *, day: int | None = None, retention: int = RETENTION_DAYS,
                 max_entries: int = MAX_ENTRIES) -> None:
        self.kvs = kvs
        self.key = key
        self.day = today() if day is None else day
        self.retention = retention
        self.max_entries = max_entries
        self.evicted = 0  # entries dropped for exceeding max_entries (load and saves of this run)
        self.jobs: dict[str, int] = {}
        self.known: frozenset[str] = frozenset()  # what earlier runs delivered (snapshot at load)
        self.pruned = 0
        self.loaded = False  # a record existed
        self._lock = asyncio.Lock()  # saves come from the loop and from the PERSIST_STATE listener

    def _cutoff(self) -> int:
        return self.day - self.retention

    async def load(self) -> None:
        raw = await self.kvs.get_value(self.key)
        jobs = raw.get("jobs") if isinstance(raw, dict) and raw.get("v") == FORMAT_VERSION else None
        if raw is not None and not isinstance(jobs, dict):
            log.warning("State record %r is not in the expected format; starting with an empty state.", self.key)
            jobs = None
        self.loaded = jobs is not None
        cutoff = self._cutoff()
        kept = {k: d for k, d in (jobs or {}).items() if isinstance(k, str) and isinstance(d, int) and d >= cutoff}
        self.pruned = len(jobs or {}) - len(kept)
        self.jobs = kept
        self._evict_over_cap()  # before anything is delivered, so the run never outgrows the cap
        self.known = frozenset(self.jobs)

    def _evict_over_cap(self) -> None:
        """Keep at most ``max_entries``: the least recently seen entries go first (ties: the
        entry that was stored first)."""
        extra = len(self.jobs) - self.max_entries
        if extra <= 0:
            return
        victims = sorted(self.jobs, key=self.jobs.__getitem__)[:extra]  # stable sort
        for key in victims:
            del self.jobs[key]
        self.evicted += extra
        log.warning("State %r holds more than %d jobs: evicted the %d least recently seen; if they are "
                    "still listed they will be delivered again.", self.key, self.max_entries, extra)

    def touch(self, key: str) -> None:
        """A job from an earlier run is still listed: refresh its last-seen day."""
        if key in self.jobs:
            self.jobs[key] = self.day

    def mark(self, key: str) -> None:
        """A job was delivered: remember it."""
        self.jobs[key] = self.day

    def mark_all(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.jobs[key] = self.day

    async def save(self) -> None:
        async with self._lock:
            cutoff = self._cutoff()
            self.jobs = {k: d for k, d in self.jobs.items() if d >= cutoff}
            self._evict_over_cap()
            await self.kvs.set_value(self.key, {"v": FORMAT_VERSION, "jobs": dict(self.jobs)})
