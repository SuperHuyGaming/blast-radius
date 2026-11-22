"""Run every test class in-process on the unpatched tree, one fork per class.

    python tools/calibrate.py SQLGLOT_DIR TESTS_JSON OUT.json

Records per-test CPU, per-class overhead (module import + class setup), and
any test that fails here although it passes under pytest (never trusted).
"""
import json, os, sys, time, marshal, resource
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src, tj, out = sys.argv[1:4]
os.environ["SKIP_INTEGRATION"] = "0"
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
sys.path.insert(0, src)
import runner  # noqa
import unittest, unittest.mock, duckdb, pandas, numpy, pandas.testing, sqlite3, pytz  # noqa
import importlib, pkgutil
import sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if not m.name.endswith("__main__"):
        try: importlib.import_module(m.name)
        except Exception: pass
tests = [t["id"] for t in json.load(open(tj))["tests"]]
groups = {}
for t in tests:
    groups.setdefault(t.rsplit("::", 1)[0], []).append(t)
result = dict(test_cpu={}, class_cpu={}, failing=[], crashed=[])
for g, ids in groups.items():
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        t0 = time.process_time()
        ct = {}
        failed, times, _ = runner.run(src, ids, lambda t: 600.0, class_times=ct)
        total = time.process_time() - t0
        os.write(w, marshal.dumps((sorted(failed), times, total)))
        os._exit(0)
    os.close(w)
    data = b""
    while True:
        b = os.read(r, 1 << 20)
        if not b: break
        data += b
    os.close(r); os.waitpid(pid, 0)
    if not data:
        result["crashed"].append(g); continue
    failed, times, total = marshal.loads(data)
    result["failing"] += failed
    result["test_cpu"].update({k: round(v, 4) for k, v in times.items()})
    result["class_cpu"][g] = round(max(0.0, total - sum(times.values())), 4)
    print(f"{g}: {len(ids)} tests {total:.2f}s fail={len(failed)}", file=sys.stderr)
json.dump(result, open(out, "w"), indent=0)
print("failing", result["failing"], "crashed", result["crashed"], "total cpu", sum(result["test_cpu"].values()) + sum(result["class_cpu"].values()))
