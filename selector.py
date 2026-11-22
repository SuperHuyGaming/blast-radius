"""Blast Radius Selector: high-precision dependency maps + dual-fork in-process verification
+ exact optimal worker packing."""

from __future__ import annotations

import json
import marshal
import os
import sys
import time
import unittest  # Must be imported before sqlglot (exp.UNITTEST = True)
import zlib
from pathlib import Path

# Prevent any .pyc writes on read-only filesystem
sys.dont_write_bytecode = True

# Protect stdout: ONLY final JSON lines may ever be written to REAL_STDOUT!
REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

from candidate_engine import CandidateEngine
from packer import duration_ms, pack_optimal
from runner import verify_batch

DATA_DIR = Path(os.environ.get("BLAST_DATA_DIR", "/data"))
SQLGLOT_ROOT = Path(os.environ.get("SQLGLOT_ROOT", "/sqlglot"))
MAPS_DIR = Path(os.environ.get("BLAST_MAPS_DIR", str(Path(__file__).resolve().parent / "maps")))


def main() -> None:
    t0 = time.perf_counter()
    import runner as _runner

    # Scale wall-clock safety deadline with batch size (180s Practice <=45 commits, 270s Final >45 commits)
    patch_paths = sorted((DATA_DIR / "commits").glob("*.patch"))
    n_commits = len(patch_paths)
    default_budget = 155.0 if n_commits <= 45 else 240.0
    budget = float(os.environ.get("BLAST_BUDGET", default_budget))
    _runner.DEADLINE = time.monotonic() + budget

    # 1. Load runtime tests.json
    tests_list: list[dict] = json.loads((DATA_DIR / "tests.json").read_text(encoding="utf-8"))["tests"]
    num_to_test = {t["number"]: t for t in tests_list}
    dur_by_num = {t["number"]: duration_ms(t["seconds"]) for t in tests_list}

    # 2. Load compact core maps (< 40ms); subtest files load lazily on demand!
    core_path = MAPS_DIR / "core_maps.zlib"
    if not core_path.exists():
        core_path = MAPS_DIR / "blast_maps.zlib"
    maps = marshal.loads(zlib.decompress(core_path.read_bytes()))
    t_load = time.perf_counter()

    # 3. Plan all commits in /data/commits/*.patch
    engine = CandidateEngine(maps, tests_list, SQLGLOT_ROOT, subtests_dir=MAPS_DIR / "subtests")
    plans = [
        engine.plan_commit(p.stem, p.read_text(encoding="utf-8", errors="replace"))
        for p in patch_paths
    ]
    t_plan = time.perf_counter()

    # 4. Verify all non-noop commits with Dual-Fork + Fail-Fast + Slack Absorption
    selected_by_commit = verify_batch(plans, maps, num_to_test, dur_by_num, SQLGLOT_ROOT, engine=engine)
    t_verify = time.perf_counter()

    # 5. Pack optimally (<= 8 workers) and emit strict JSONL to REAL_STDOUT
    lines: list[str] = []
    total_sim_ms = 0
    for p in plans:
        chosen = selected_by_commit.get(p.commit, [])
        cost_ms, workers = pack_optimal(chosen, dur_by_num)
        total_sim_ms += cost_ms
        lines.append(json.dumps({"commit": p.commit, "workers": workers}, separators=(",", ":")))

    REAL_STDOUT.write("\n".join(lines) + ("\n" if lines else ""))
    REAL_STDOUT.flush()

    t_end = time.perf_counter()
    grp_counts: dict[str, int] = {}
    for p in plans:
        grp_counts[p.group] = grp_counts.get(p.group, 0) + 1
    print(
        f"[blast] commits={len(plans)} groups={grp_counts} sim={total_sim_ms/1000:.3f}s "
        f"wall={t_end-t0:.3f}s (load={t_load-t0:.3f}s plan={t_plan-t_load:.3f}s verify={t_verify-t_plan:.3f}s)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
