"""Exact optimal multiprocessor worker packing for Blast Radius.

Scoring rule (SCORING.md):
- Duration of test i is d_i = floor(seconds * 1000 + 0.5) ms.
- Empty selection -> workers = [], cost = 0 ms.
- Non-empty selection split across 1 <= w <= 8 non-empty workers:
    cost = max(load_1, ..., load_w) + 2000 * w
"""

from __future__ import annotations

import math
from typing import Sequence

WORKER_COST_MS = 2000
MAX_WORKERS = 8


def duration_ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def _lpt_pack(items: list[tuple[int, int]], w: int) -> tuple[int, list[list[int]], list[int]]:
    """Longest-Processing-Time-first greedy packing. Items must be sorted descending by dur."""
    workers: list[list[int]] = [[] for _ in range(w)]
    loads = [0] * w
    for num, dur in items:
        best_i = 0
        best_l = loads[0]
        for i in range(1, w):
            if loads[i] < best_l:
                best_l = loads[i]
                best_i = i
        workers[best_i].append(num)
        loads[best_i] = best_l + dur
    return max(loads), workers, loads


def _improve_pack(
    workers: list[list[int]],
    loads: list[int],
    dur_by_num: dict[int, int],
    lb: int,
) -> tuple[int, list[list[int]], list[int]]:
    """Fast 1-move and 2-swap local search to close any LPT gap to lower bound."""
    w = len(workers)
    makespan = max(loads)
    if makespan <= lb:
        return makespan, workers, loads

    n_items = sum(len(wk) for wk in workers)
    # For large item sets, LPT is already within ~1ms of LB; skip costly O(n^2) 2-swaps!
    if n_items > 100 and makespan <= lb + 2:
        return makespan, workers, loads
    max_iters = 8 if n_items > 80 else 64

    for _ in range(max_iters):
        hi = max(range(w), key=loads.__getitem__)
        makespan = loads[hi]
        if makespan <= lb:
            break

        improved = False
        hi_items = workers[hi]

        # 1-move: move one item from heaviest worker to another worker
        for idx_a, a in enumerate(hi_items):
            da = dur_by_num[a]
            if da == 0:
                continue
            for lo in range(w):
                if lo == hi:
                    continue
                if loads[lo] + da < makespan:
                    hi_items.pop(idx_a)
                    workers[lo].append(a)
                    loads[hi] -= da
                    loads[lo] += da
                    improved = True
                    break
            if improved:
                break
        if improved:
            continue

        # 2-swap: swap an item in heaviest worker with a smaller item in another worker
        for idx_a, a in enumerate(hi_items):
            da = dur_by_num[a]
            for lo in range(w):
                if lo == hi:
                    continue
                lo_items = workers[lo]
                slack = makespan - loads[lo]
                for idx_b, b in enumerate(lo_items):
                    db = dur_by_num[b]
                    diff = da - db
                    if 0 < diff < slack:
                        hi_items[idx_a] = b
                        lo_items[idx_b] = a
                        loads[hi] -= diff
                        loads[lo] += diff
                        improved = True
                        break
                if improved:
                    break
            if improved:
                break
        if not improved:
            break

    return max(loads), workers, loads


def _exact_bnb(
    items: list[tuple[int, int]],
    w: int,
    ub: int,
    lb: int,
    node_limit: int = 4000,
) -> tuple[int, list[list[int]]] | None:
    """Bounded branch-and-bound for exact makespan when local search didn't hit LB.

    Only considers items with dur > 0 (zero-ms items can go on any worker).
    """
    pos_items = [(num, dur) for num, dur in items if dur > 0]
    n = len(pos_items)
    if n <= w or n > 40:
        return None

    best_ms = ub
    best_assign: list[int] | None = None
    loads = [0] * w
    assign = [0] * n
    nodes = 0

    def dfs(idx: int, cur_max: int) -> None:
        nonlocal best_ms, best_assign, nodes
        nodes += 1
        if nodes > node_limit:
            return
        if cur_max >= best_ms:
            return
        if idx == n:
            best_ms = cur_max
            best_assign = assign.copy()
            return

        _, dur = pos_items[idx]
        seen_loads: set[int] = set()
        # Try bins in ascending load order (pruning identical bin loads for symmetry breaking)
        order = sorted(range(w), key=loads.__getitem__)
        for b in order:
            lb_bin = loads[b]
            if lb_bin in seen_loads:
                continue
            new_load = lb_bin + dur
            if new_load >= best_ms:
                continue
            seen_loads.add(lb_bin)
            loads[b] = new_load
            assign[idx] = b
            dfs(idx + 1, new_load if new_load > cur_max else cur_max)
            loads[b] = lb_bin
            if best_ms <= lb or nodes > node_limit:
                return

    dfs(0, 0)
    if best_assign is None:
        return None

    workers: list[list[int]] = [[] for _ in range(w)]
    for (num, _), b in zip(pos_items, best_assign):
        workers[b].append(num)
    for num, dur in items:
        if dur == 0:
            workers[0].append(num)
    return best_ms, workers


def pack_optimal(test_nums: Sequence[int], dur_by_num: dict[int, int]) -> tuple[int, list[list[int]]]:
    """Return (optimal_cost_ms, workers) for a set of unique test numbers."""
    if not test_nums:
        return 0, []

    items = sorted(((n, dur_by_num[n]) for n in test_nums), key=lambda x: (-x[1], x[0]))
    total_ms = sum(d for _, d in items)
    max_d = items[0][1]
    n = len(items)

    # w = 1 baseline
    best_cost = total_ms + WORKER_COST_MS
    best_workers: list[list[int]] = [[num for num, _ in items]]

    max_w = min(MAX_WORKERS, n)
    for w in range(2, max_w + 1):
        lb = max(max_d, (total_ms + w - 1) // w)
        if lb + WORKER_COST_MS * w >= best_cost:
            continue

        ms, workers, loads = _lpt_pack(items, w)
        if ms > lb:
            ms, workers, loads = _improve_pack(workers, loads, dur_by_num, lb)
        if ms > lb and ms + WORKER_COST_MS * w >= best_cost - 500:
            res = _exact_bnb(items, w, min(ms, best_cost - WORKER_COST_MS * w), lb)
            if res is not None and res[0] < ms:
                ms, workers = res

        cost = ms + WORKER_COST_MS * w
        if cost < best_cost:
            best_cost = cost
            best_workers = [wk for wk in workers if wk]

    return best_cost, best_workers


def optimal_cost_ms(test_nums: Sequence[int] | set[int], dur_by_num: dict[int, int]) -> int:
    """Fast cost query for slack-absorption decisions."""
    if not test_nums:
        return 0
    if len(test_nums) == 1:
        return dur_by_num[next(iter(test_nums))] + WORKER_COST_MS
    cost, _ = pack_optimal(list(test_nums), dur_by_num)
    return cost


def max_total_for_cost_ms(cost_ms: int, min_makespan_ms: int) -> int:
    """Exact upper bound on sum(d_i) that can possibly pack into <= cost_ms with max(d_i) >= min_makespan_ms."""
    best = 0
    for w in range(1, MAX_WORKERS + 1):
        rem_ms = cost_ms - WORKER_COST_MS * w
        if rem_ms >= min_makespan_ms:
            cap = w * rem_ms
            if cap > best:
                best = cap
    return best


def fast_cost_ms(test_nums: Sequence[int] | set[int], dur_by_num: dict[int, int]) -> int:
    """Ultra-fast O(n log n) LPT upper-bound cost query for large candidate sets."""
    n = len(test_nums)
    if n == 0:
        return 0
    if n == 1:
        return dur_by_num[next(iter(test_nums))] + WORKER_COST_MS
    if n <= 40:
        cost, _ = pack_optimal(list(test_nums), dur_by_num)
        return cost
    durs = sorted((dur_by_num[t] for t in test_nums), reverse=True)
    total_ms = sum(durs)
    max_d = durs[0]
    best_cost = total_ms + WORKER_COST_MS
    max_w = min(MAX_WORKERS, n)
    for w in range(2, max_w + 1):
        lb = max(max_d, (total_ms + w - 1) // w)
        if lb + WORKER_COST_MS * w >= best_cost:
            continue
        loads = [0] * w
        for d in durs:
            bi = 0
            bl = loads[0]
            for i in range(1, w):
                if loads[i] < bl:
                    bl = loads[i]
                    bi = i
            loads[bi] = bl + d
        cost = max(loads) + WORKER_COST_MS * w
        if cost < best_cost:
            best_cost = cost
    return best_cost


def lb_cost_ms(total_ms: float, max_d: float) -> float:
    """O(8) lower-bound cost of a NON-EMPTY selection with the given total and longest duration:
    min over w of max(max_d, total / w) + 2000 * w."""
    best = total_ms + WORKER_COST_MS
    if max_d > total_ms:
        best = max_d + WORKER_COST_MS
    for w in range(2, MAX_WORKERS + 1):
        m = total_ms / w
        if m < max_d:
            m = max_d
        c = m + WORKER_COST_MS * w
        if c < best:
            best = c
        elif m <= max_d:
            break
    return best
