"""Records the unit map of test_execute_tpcds at the pinned commit: maps/units.bin.

usage: rec_units.py all <sqlglot checkout> <work dir> [jobs]   every step below
       rec_units.py unit <sqlglot checkout> <work dir> <i>     line coverage of query i
       rec_units.py time <sqlglot checkout> <work dir>         CPU per query, DuckDB results
       rec_units.py build <sqlglot checkout> <work dir> <out file> [scale]   merge into units.bin

Each query is recorded in its own fresh process, so the lines that fill
process-wide caches show up under every query that needs them, as they would
in whichever pool worker runs it first.
"""

import json
import os
import pickle
import subprocess
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import subrun  # noqa: E402

FORK_MS = 200  # overlay, fork and compiling the patched modules: not seen by `time`


def count_units(root):
    sys.path.insert(0, root)
    from tests.helpers import load_sql_fixture_pairs

    return len(list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")))


def record_unit(root, work, i):
    import coverage

    cov = coverage.Coverage(data_file=None, source=[os.path.join(root, "sqlglot")], branch=False)
    cov.start()
    cov.switch_context("pre")

    def start_units():
        cov.switch_context("run")
        # sysmon reports a line once per process: re-arm so "run" sees lines "pre" ran
        if hasattr(sys, "monitoring"):
            sys.monitoring.restart_events()

    ok = subrun.run_units(root, [i], {}, count_units(root), hook=start_units)
    cov.stop()
    data = cov.get_data()
    out = {"ok": ok, "pre": {}, "run": {}}
    for f in data.measured_files():
        rel = os.path.relpath(os.path.realpath(f), root)
        for ln, ctxs in data.contexts_by_lineno(f).items():
            for c in ctxs:
                if c in out:
                    out[c].setdefault(rel, []).append(ln)
    with open(os.path.join(work, f"u{i}.json"), "w") as f:
        json.dump(out, f)
    if not ok:
        sys.exit(f"unit {i} failed on the pinned tree")


def record_time(root, work):
    import duckdb  # noqa: F401
    import pandas.testing  # noqa: F401

    n = count_units(root)
    subrun.save_rows(root, subrun.ROWS)
    with open(subrun.ROWS, "rb") as f:
        rows = pickle.load(f)
    # in a fork, as a run is, before this process imports the tests
    out, fixed_rows = subrun.fork_run(root, {}, lambda tree, w: os.write(w, b"P" if subrun.run_units(tree, [], {}, n, rows=rows) else b"F"), 60, 120, bool)
    assert out == b"P"
    cpu = time.process_time
    t = cpu()
    assert subrun.run_units(root, [], {}, n)
    fixed = cpu() - t
    frames = {}
    assert subrun.run_units(root, list(range(n)), {}, n, capture=frames)
    started = [0.0]

    def mark():
        started[0] = cpu()

    def one(keep):
        best = None
        for _ in range(3):
            assert subrun.run_units(root, keep, frames, n, hook=mark)
            took = cpu() - started[0]
            best = took if best is None else min(best, took)
        return best

    empty = one([])
    ms = [max(1, round((one([i]) - empty) * 1000)) for i in range(n)]
    with open(os.path.join(work, "time.pkl"), "wb") as f:
        pickle.dump({"n": n, "ms": ms, "fixed_ms": round(fixed * 1000) + FORK_MS, "fixed_rows_ms": round(fixed_rows * 1000) + FORK_MS, "frames": frames}, f)
    print(f"fixed={fixed:.2f}s units={sum(ms) / 1000:.2f}s frames={len(frames)}", file=sys.stderr)


def build(root, work, out, scale):
    from build_maps import body_lines

    with open(os.path.join(work, "time.pkl"), "rb") as f:
        t = pickle.load(f)
    lines = {}
    pre = {}
    bodies = {}
    for i in range(t["n"]):
        with open(os.path.join(work, f"u{i}.json")) as f:
            u = json.load(f)
        assert u["ok"], i
        for rel, lns in u["run"].items():
            d = lines.setdefault(rel, {})
            if rel not in bodies:
                with open(os.path.join(root, rel), encoding="utf-8") as f:
                    bodies[rel] = body_lines(f.read(), rel)
            for ln in lns:
                if ln in bodies[rel]:
                    continue  # module and class bodies run at import: the selector reads them through clsmap
                d[ln] = d.get(ln, 0) | (1 << i)
        for rel, lns in u["pre"].items():
            pre.setdefault(rel, set()).update(lns)
    units = {
        "test": subrun.TEST_ID,
        "n": t["n"],
        "lines": lines,
        "pre": {rel: sorted(lns) for rel, lns in pre.items()},
        "ms": t["ms"],
        "fixed_ms": t["fixed_ms"],
        "fixed_rows_ms": t["fixed_rows_ms"],
        "scale": scale,
        "frames": pickle.dumps(t["frames"]),  # unpickling needs pandas: left to the runner
    }
    blob = zlib.compress(pickle.dumps(units), 9)
    with open(out, "wb") as f:
        f.write(blob)
    print(f"units={t['n']} files={len(lines)} lines={sum(len(d) for d in lines.values())} bytes={len(blob)}", file=sys.stderr)


def main():
    mode = sys.argv[1]
    sys.dont_write_bytecode = True  # the checkout stays as fetched
    if mode == "build":
        build(sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5]) if len(sys.argv) > 5 else 1.2)
        return
    root = os.path.realpath(sys.argv[2])
    work = sys.argv[3]
    os.makedirs(work, exist_ok=True)
    if mode == "unit":
        record_unit(root, work, int(sys.argv[4]))
    elif mode == "time":
        record_time(root, work)
    else:
        jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 2
        me = [sys.executable, os.path.abspath(__file__)]
        with ThreadPoolExecutor(jobs) as pool:
            codes = list(pool.map(lambda i: subprocess.run(me + ["unit", root, work, str(i)]).returncode, range(count_units(root))))
        if any(codes):
            sys.exit(f"failed units: {[i for i, c in enumerate(codes) if c]}")
        subprocess.run(me + ["time", root, work], check=True)


if __name__ == "__main__":
    main()
