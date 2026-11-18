"""Run many companies concurrently and merge results fairly (no Apify imports)."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable, Container, Sequence

from .ats import ADAPTERS, CompanyRef, Job, JobFilter, RateLimitedClient, RunReport

log = logging.getLogger("jobfeed")
_DONE = object()

# How a company's feed ended, as reported through ``iter_jobs(outcomes=...)``.
COMPLETED = "completed"  # the feed was seen to end (possibly with zero matching jobs)
ERRORED = "error"        # the fetch raised (jobs delivered before that are kept)
STOPPED = "stopped"      # iter_jobs ended (cap/budget/consumer) before the feed was seen to end


def fair_caps(delivered: Sequence[int], final: Sequence[bool], total: int) -> list[int]:
    """Max number of jobs each company may have delivered, given ``total`` slots overall.

    Water-filling: a finished company (``final``) is worth exactly what it delivered;
    the remaining slots are split evenly over the unfinished ones, so capacity a
    short company leaves unused goes to those that still have jobs. When the slots do
    not divide evenly, the spare ones (``total % open``) are first come, first served:
    an unfinished company that has already taken one keeps it, the others may still
    claim the rest. A company holding more than its fair share (e.g. after a restart)
    is frozen where it is and the rest is shared among the others.
    """
    n = len(delivered)
    caps: list[int | None] = [d if f else None for d, f in zip(delivered, final)]
    while True:
        open_ = [i for i in range(n) if caps[i] is None]
        if not open_:
            return caps  # type: ignore[return-value]
        rem = max(0, total - sum(c for c in caps if c is not None))
        level, extra = divmod(rem, len(open_))
        claimed = [i for i in open_ if delivered[i] > level]
        keep = [i for i in claimed if delivered[i] == level + 1][:extra]
        frozen = [i for i in claimed if i not in keep]
        if frozen:
            for i in frozen:
                caps[i] = delivered[i]
            continue
        free = extra - len(keep)
        for i in open_:
            caps[i] = level + 1 if (i in keep or free > 0) else level


def job_key(job: Job) -> str:
    """Stable identity of a job across restarts (JSON-friendly)."""
    return f"{job.atsSource}|{job.companySlug or ''}|{job.jobId}"


async def fetch_company(
    client: RateLimitedClient,
    ref: CompanyRef,
    flt: JobFilter,
    include_description: bool = True,
    report: RunReport | None = None,
) -> AsyncIterator[Job]:
    async for job in ADAPTERS[ref.ats].fetch_jobs(
        client, ref, flt, include_description, report=report
    ):
        yield job


async def iter_jobs(
    client: RateLimitedClient,
    refs: list[CompanyRef],
    flt: JobFilter,
    *,
    include_description: bool = True,
    max_items: int | None = None,
    on_error: Callable[[CompanyRef, Exception], None] | None = None,
    queue_size: int = 5,
    seen: set[str] | None = None,
    report: RunReport | None = None,
    prior: Sequence[int] | None = None,
    outcomes: dict[str, str] | None = None,
    known: Container[str] | None = None,
    on_known: Callable[[Job], None] | None = None,
) -> AsyncIterator[Job]:
    """Yield jobs as they become ready, at most ``max_items`` in total, split fairly.

    Each company runs in its own task feeding a small bounded queue, so lazy
    sources (Workday) only crawl as far as needed. The consumer never waits on one
    particular company: it takes the next item from whichever queues are ready
    (rotating, one item per queue per pass) and only sleeps when every queue is
    empty. A slow or retrying company therefore delays nobody else, and its items
    are yielded the moment they arrive. A failing company is reported through
    ``on_error`` and skipped.

    Fair split (:func:`fair_caps`): while a company has not finished, nobody may
    take more than an even share of the slots. A company that ends with fewer jobs
    than its share (including none, or an error) hands its leftover to the companies
    that still have jobs; their surplus is held back in the queues until then. So with
    ``max_items >= len(refs)`` every company that has a matching job gets at least
    one, and a slow company keeps its share reserved while it is fetched.

    ``seen`` holds job keys (:func:`job_key`) to skip, e.g. restored after a
    restart; yielded keys are added to it. ``max_items`` counts new jobs only.
    ``prior[i]`` is how many jobs ``refs[i]`` already delivered before this call
    (a restart); they count toward the share, so the split over the whole run stays
    fair. If ``outcomes`` is given it is filled, when the generator ends, with one of
    :data:`COMPLETED` / :data:`ERRORED` / :data:`STOPPED` per ``ref.key``.

    ``known`` (incremental mode) holds job keys delivered by *earlier runs*: such a job is
    dropped when it comes off the queue, before the fair-split bookkeeping, so it uses up
    no share and no ``max_items`` slot. ``on_known(job)`` is called for each one (to refresh
    its last-seen day). Keys already in ``seen`` (delivered by this run) win over ``known``,
    so they are not reported as known.
    """
    seen = seen if seen is not None else set()
    n = len(refs)
    delivered = list(prior) if prior is not None else [0] * n
    total = sum(delivered) + max_items if max_items is not None else None
    queues = [asyncio.Queue(maxsize=queue_size) for _ in refs]
    heads: list[Job | None] = [None] * n  # item taken from the queue but not yet delivered
    finished = [False] * n
    errored = [False] * n
    wake = asyncio.Event()

    async def worker(i: int) -> None:
        ref, q = refs[i], queues[i]
        try:
            async for job in fetch_company(client, ref, flt, include_description, report):
                await q.put(job)
                wake.set()
        except Exception as exc:  # per-company failures are never fatal
            log.error("%s failed: %s: %s", ref.key, type(exc).__name__, exc)
            errored[i] = True
            if on_error:
                on_error(ref, exc)
        # tell the consumer this source is finished (skipped on cancellation)
        await q.put(_DONE)
        wake.set()

    def refill(i: int) -> bool:
        """Move the next unseen queued item into ``heads[i]``; note when the source is finished.
        Returns True if it consumed anything (an item or the end marker)."""
        moved = False
        while not finished[i] and heads[i] is None and not queues[i].empty():
            moved = True
            item = queues[i].get_nowait()
            if item is _DONE:
                finished[i] = True
            else:
                key = job_key(item)
                if key in seen:
                    continue
                if known is not None and key in known:
                    if on_known:
                        on_known(item)
                    continue
                heads[i] = item
        return moved

    def caps() -> list[int] | None:
        return None if total is None else fair_caps(delivered, finished, total)

    tasks = [asyncio.create_task(worker(i)) for i in range(n)]
    count = 0
    start = 0
    try:
        while not all(finished) and (max_items is None or count < max_items):
            wake.clear()  # anything queued after this point sets it again
            progressed = False
            for i in range(n):
                progressed |= refill(i)
            order = [(i + start) % n for i in range(n)]
            for i in order:
                # identity is rechecked right before delivery: another head (e.g. a second
                # Workday site of the same tenant) may have yielded this job since it was
                # queued. Stale duplicates are dropped without consuming the share.
                while heads[i] is not None and job_key(heads[i]) in seen:
                    heads[i] = None
                    progressed = True
                    refill(i)
                if finished[i] or heads[i] is None:
                    continue
                c = caps()
                if c is not None and delivered[i] >= c[i]:
                    continue  # at its share while others may still need capacity
                item, heads[i] = heads[i], None
                seen.add(job_key(item))
                delivered[i] += 1
                count += 1
                progressed = True
                yield item
                if max_items is not None and count >= max_items:
                    return
                refill(i)
            start += 1
            if progressed:
                continue
            c = caps()
            can_serve = c is None or any(
                not finished[i] and delivered[i] < c[i] for i in range(n))
            if not can_serve:
                break  # every unfinished company sits at its share and nothing can be redistributed
            await wake.wait()
    finally:
        if outcomes is not None:
            for i, ref in enumerate(refs):
                refill(i)  # a source whose end marker is already queued did complete
                if errored[i]:  # an observed failure wins even if buffered surplus was never consumed
                    outcomes[ref.key] = ERRORED
                elif not finished[i]:
                    outcomes[ref.key] = STOPPED
                else:
                    outcomes[ref.key] = COMPLETED
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
