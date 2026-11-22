"""Per-fixture-entry coverage for tests that iterate fixture files.

For each such test, run it once with every fixture loader filtered to nothing
("base"), then once per entry k with only entry k let through. Coverage of each
run is recorded, so a sliced run at scoring time executes exactly what was mapped.

Usage: python unitmap.py <sqlglot root> <out.json> <test id> [<test id> ...]
"""

import json
import os
import pickle
import sys
import unittest

ROOT = os.path.abspath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2])
TESTS = sys.argv[3:]
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("SKIP_INTEGRATION", "0")

import coverage  # noqa: E402

cov = coverage.Coverage(data_file=None, source=[os.path.join(ROOT, "sqlglot")], branch=False)
cov.start()

import concurrent.futures as cf  # noqa: E402


class SyncExecutor:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *a, **k):
        f = cf.Future()
        try:
            a, k = pickle.loads(pickle.dumps((a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f

    def shutdown(self, *a, **k):
        pass


cf.ProcessPoolExecutor = SyncExecutor

import tests.helpers as H  # noqa: E402

FILTER = {}  # filename -> set of allowed indexes, or None for all
COUNTS = {}
_orig_pairs = H.load_sql_fixture_pairs
_orig_fix = H.load_sql_fixtures


def _wrap(orig):
    def gen(filename):
        allowed = FILTER.get(filename, None) if FILTER.get("__all__") is None else FILTER.get(filename, set())
        n = 0
        for k, item in enumerate(orig(filename)):
            n = k + 1
            if allowed is None or k in allowed:
                yield item
        COUNTS[filename] = max(COUNTS.get(filename, 0), n)

    return gen


H.load_sql_fixture_pairs = _wrap(_orig_pairs)
H.load_sql_fixtures = _wrap(_orig_fix)

CLASS_LISTS = {"tpch_sqls": "optimizer/tpc-h/tpc-h.sql", "tpcds_sqls": "optimizer/tpc-ds/tpc-ds.sql"}

result = {}


def run_one(cls, meth):
    r = unittest.TestResult()
    t = cls(meth)
    t.run(r)
    return not (r.failures or r.errors)


def lines_of(ctx):
    data = cov.get_data()
    data.set_query_context(ctx)
    out = {}
    for f in data.measured_files():
        ls = data.lines(f)
        if ls:
            out[os.path.relpath(f, ROOT).replace("\\", "/")] = sorted(ls)
    return out


by_class = {}
for tid in TESTS:
    f, cls, meth = tid.split("::")
    by_class.setdefault((f, cls), []).append(meth)

for (f, cname), meths in by_class.items():
    mod = __import__(f[:-3].replace("/", "."), fromlist=["*"])
    C = getattr(mod, cname)
    cov.switch_context("setup")
    if hasattr(C, "setUpClass"):
        C.setUpClass()
    full_lists = {a: list(getattr(C, a)) for a in CLASS_LISTS if hasattr(C, a)}
    for meth in meths:
        tid = f"{f}::{cname}::{meth}"
        # discover fixture files and counts with a normal run
        FILTER.clear()
        COUNTS.clear()
        for a, lst in full_lists.items():
            setattr(C, a, lst)
        cov.switch_context("discover|" + tid)
        ok = run_one(C, meth)
        files = dict(COUNTS)
        for a, lst in full_lists.items():
            files[CLASS_LISTS[a]] = len(lst)
        if not files:
            continue
        # base run: every loader yields nothing
        FILTER.clear()
        FILTER["__all__"] = True
        for a in full_lists:
            setattr(C, a, [])
        if isinstance(C.__dict__.get("cache"), dict):
            C.cache = {}
        cov.switch_context("base|" + tid)
        base_ok = run_one(C, meth)
        units = []
        for fn, n in files.items():
            for k in range(n):
                FILTER.clear()
                FILTER["__all__"] = True
                FILTER[fn] = {k}
                for a, lst in full_lists.items():
                    setattr(C, a, [lst[k]] if CLASS_LISTS[a] == fn else [])
                ctx = f"u|{tid}|{fn}|{k}"
                if isinstance(C.__dict__.get("cache"), dict):
                    C.cache = {}  # cold cache, as in a fresh child at scoring time
                cov.switch_context(ctx)
                passed = run_one(C, meth)
                units.append((fn, k, ctx, passed))
        result[tid] = {"files": files, "units": units, "ok": ok and base_ok}
        print("mapped", tid, files, file=sys.stderr, flush=True)
    for a, lst in full_lists.items():
        setattr(C, a, lst)

cov.stop()
final = {}
for tid, info in result.items():
    base = lines_of("base|" + tid)
    us = []
    for fn, k, ctx, passed in info["units"]:
        ls = lines_of(ctx)
        # keep only lines not already executed by the base run
        diff = {}
        for f, l in ls.items():
            b = set(base.get(f, ()))
            extra = [x for x in l if x not in b]
            if extra:
                diff[f] = extra
        us.append([fn, k, passed, diff])
    final[tid] = {"files": info["files"], "ok": info["ok"], "base": base, "units": us}
with open(OUT, "w") as fh:
    json.dump(final, fh)
