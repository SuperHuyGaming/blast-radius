"""Blast Radius selector.

Runs every test on every commit, packed longest-first onto the worker count
that makes the busiest worker plus 2 s per worker as small as possible.
"""

import json
import sys
from pathlib import Path

DATA = Path("/data")
MAX_WORKERS = 8
WORKER_COST_MS = 2000


def pack(tests: list[tuple[int, int]], count: int) -> tuple[int, list[list[int]]]:
    """Longest test first onto the least-loaded worker. Returns (cost_ms, workers)."""
    workers: list[list[int]] = [[] for _ in range(count)]
    loads = [0] * count
    for number, ms in sorted(tests, key=lambda t: -t[1]):
        i = loads.index(min(loads))
        workers[i].append(number)
        loads[i] += ms
    workers = [w for w in workers if w]
    return max(loads) + WORKER_COST_MS * len(workers), workers


def best_split(tests: list[tuple[int, int]]) -> list[list[int]]:
    if not tests:
        return []
    return min((pack(tests, k) for k in range(1, MAX_WORKERS + 1)), key=lambda r: r[0])[1]


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    durations = [(t["number"], int(t["seconds"] * 1000 + 0.5)) for t in tests]
    workers = best_split(durations)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"selected all {len(tests)} tests on {len(workers)} workers", file=sys.stderr)


if __name__ == "__main__":
    main()
