"""Record the CPU of running each test alone, and of collecting each test file,
through subrun's forked pytest on the pristine tree: SuiteRunner's estimates.

usage: testcpu.py <shard> <shards> <out.json>     (PYTHON_CPU_COUNT=2, on the Mac)
       testcpu.py merge <maps/testcpu.json> <shard.json>...
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import subrun  # noqa: E402

SRC = os.path.expanduser("~/ws/buildathon/work/sqlglot")


def collect(tree, w, path):
    import pytest

    os.chdir(tree)
    sys.path.insert(0, tree)
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    rc = pytest.main(["-p", "no:cacheprovider", "-q", "--collect-only", "--rootdir", tree, path])
    os.write(w, b"D\n" if rc == 0 else b"X\n")


def main():
    if sys.argv[1] == "merge":
        out = {"file": {}, "test": {}}
        parts = [json.load(open(p)) for p in sys.argv[3:]]
        for part in parts:
            out["file"].update(part["file"])
            out["test"].update(part["test"])
        for key in ("bench", "warm", "heavy"):
            out[key] = min(part[key] for part in parts)
        with open(sys.argv[2], "w") as f:
            json.dump(out, f, separators=(",", ":"), sort_keys=True)
        return
    shard, shards = int(sys.argv[1]), int(sys.argv[2])
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    tests = json.load(open(os.path.join(HERE, "sample", "tests.json")))["tests"]
    files = sorted({t["file"] for t in tests})
    res = {"file": {}, "test": {}, "bench": subrun.bench()}
    # the selector's forks start with these loaded, HEAVY only for the files that import it
    t0 = time.process_time()
    subrun.warm(subrun.WARM)
    res["warm"] = round(time.process_time() - t0, 4)
    done = lambda buf: buf.endswith(b"\n")  # noqa: E731
    for heavy in (False, True):
        if heavy:
            t0 = time.process_time()
            subrun.warm(subrun.HEAVY)
            res["heavy"] = round(time.process_time() - t0, 4)
        for i, path in enumerate(files):
            if i % shards == shard and (path in subrun.HEAVY_FILES) == heavy:
                out, cpu = subrun.fork_run(SRC, {}, lambda tree, w: collect(tree, w, path), 600, 600, done)
                res["file"][path] = round(cpu, 4) if out == b"D\n" else 9.0
        record(tests, shard, shards, heavy, res)
    with open(sys.argv[3], "w") as f:
        json.dump(res, f)


def record(tests, shard, shards, heavy, res):
    for t in tests:
        if t["number"] % shards == shard and (t["file"] in subrun.HEAVY_FILES) == heavy:
            rows = subrun.load_rows() if subrun.rows_ok({}) and t["id"].split("::")[:2] == ["tests/test_executor.py", subrun.EXECUTOR] else None
            out, cpu = subrun.fork_run(SRC, {}, lambda tree, w: subrun.run_suite(tree, [t["id"]], w, rows), 600, 600, lambda b: b.endswith((b"D\n", b"X\n")))
            ok = out == b"P 0\nD\n"
            res["test"][t["number"]] = round(cpu, 4) if ok else 9.0
            if not ok:
                print(t["id"], out, file=sys.stderr)


if __name__ == "__main__":
    main()
