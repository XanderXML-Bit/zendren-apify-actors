"""Run many boards concurrently and merge results fairly (no Apify imports)."""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable, Sequence

from . import greenhouse
from .greenhouse import JobFilter, Options
from .http import RateLimitedClient
from .models import BoardRef, Job, RunReport

log = logging.getLogger("ghfeed")
_DONE = object()

# How a board's feed ended, as reported through ``iter_jobs(outcomes=...)``.
COMPLETED = "completed"  # the feed was seen to end (possibly with zero matching jobs)
ERRORED = "error"        # the fetch raised (jobs delivered before that are kept)
STOPPED = "stopped"      # iter_jobs ended (cap/budget/consumer) before the feed was seen to end


def fair_caps(delivered: Sequence[int], final: Sequence[bool], total: int) -> list[int]:
    """Max number of jobs each board may have delivered, given ``total`` slots overall.

    Water-filling: a finished board (``final``) is worth exactly what it delivered;
    the remaining slots are split evenly over the unfinished ones, so capacity a
    short board leaves unused goes to those that still have jobs. When the slots do
    not divide evenly, the spare ones (``total % open``) are first come, first served:
    an unfinished board that has already taken one keeps it, the others may still
    claim the rest. A board holding more than its fair share (e.g. after a restart)
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


def fetch_board(client, ref, flt, opts, **kw):
    """Indirection point for the per-board job source (replaced by fakes in tests)."""
    return greenhouse.fetch_jobs(client, ref, flt, opts, **kw)


async def iter_jobs(
    client: RateLimitedClient,
    refs: list[BoardRef],
    flt: JobFilter,
    opts: Options,
    *,
    max_items: int | None = None,
    on_error: Callable[[BoardRef, Exception], None] | None = None,
    queue_size: int = 5,
    seen: set[str] | None = None,
    known: set[str] | None = None,
    touch: Callable[[str], None] | None = None,
    max_parallel_boards: int = 3,
    report: RunReport | None = None,
    prior: Sequence[int] | None = None,
    outcomes: dict[str, str] | None = None,
) -> AsyncIterator[Job]:
    """Yield jobs as they become ready, at most ``max_items`` in total, split fairly.

    Each board runs in its own task feeding a small bounded queue, so a
    board's jobs are only built (detail requests included) as far as needed. The consumer never waits on one
    particular board: it takes the next item from whichever queues are ready
    (rotating, one item per queue per pass) and only sleeps when every queue is
    empty. A slow or retrying board therefore delays nobody else, and its items
    are yielded the moment they arrive. A failing board is reported through
    ``on_error`` and skipped.

    Fair split (:func:`fair_caps`): while a board has not finished, nobody may
    take more than an even share of the slots. A board that ends with fewer jobs
    than its share (including none, or an error) hands its leftover to the boards
    that still have jobs; their surplus is held back in the queues until then. So with
    ``max_items >= len(refs)`` every board that has a matching job gets at least
    one, and a slow board keeps its share reserved while it is fetched.

    ``seen`` holds job keys (``Job.key``, ``<token>|<id>``) to skip, e.g. restored after a
    restart; yielded keys are added to it. ``max_items`` counts new jobs only.
    ``prior[i]`` is how many jobs ``refs[i]`` already delivered before this call
    (a restart); they count toward the share, so the split over the whole run stays
    fair. If ``outcomes`` is given it is filled, when the generator ends, with one of
    :data:`COMPLETED` / :data:`ERRORED` / :data:`STOPPED` per ``ref.key``.
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
    gate = asyncio.Semaphore(max_parallel_boards)

    async def worker(i: int) -> None:
        ref, q = refs[i], queues[i]
        try:
            async for job in fetch_board(client, ref, flt, opts, seen=seen, known=known, touch=touch,
                                         report=report, board_gate=gate):
                await q.put(job)
                wake.set()
        except Exception as exc:  # per-board failures are never fatal
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
            elif item.key not in seen:
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
                # identity is rechecked right before delivery: another head (e.g. a board listed
                # twice under different spellings) may have yielded this job since it was
                # queued. Stale duplicates are dropped without consuming the share.
                while heads[i] is not None and heads[i].key in seen:
                    heads[i] = None
                    progressed = True
                    refill(i)
                if finished[i] or heads[i] is None:
                    continue
                c = caps()
                if c is not None and delivered[i] >= c[i]:
                    continue  # at its share while others may still need capacity
                item, heads[i] = heads[i], None
                seen.add(item.key)
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
                break  # every unfinished board sits at its share and nothing can be redistributed
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
