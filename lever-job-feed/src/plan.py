"""Fair split of ``maxItems`` across sites."""

from __future__ import annotations


def fair_split(capacity: list[int], total: int) -> list[int]:
    """Max-min fair allocation: every site gets an equal share, sites with fewer postings than
    their share hand the surplus to the others; remainders go to the earlier sites.

    ``capacity[i]`` is how many postings site ``i`` can deliver; the result never exceeds it
    and sums to ``min(total, sum(capacity))``."""
    alloc = [0] * len(capacity)
    active = [i for i, c in enumerate(capacity) if c > 0]
    left = max(0, total)
    while active and left > 0:
        share, extra = divmod(left, len(active))
        if share == 0:
            for i in active[:left]:
                alloc[i] += 1
            break
        small = [i for i in active if capacity[i] - alloc[i] <= share]
        if small:
            for i in small:
                left -= capacity[i] - alloc[i]
                alloc[i] = capacity[i]
            active = [i for i in active if i not in small]
        else:
            for k, i in enumerate(active):
                alloc[i] += share + (1 if k < extra else 0)
            break
    return alloc
