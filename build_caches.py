"""Build-time cache generator (runs inside Dockerfile, adds 0 bytes to git repo).

Precomputes:
1. /app/fixture_cache.pkl.zlib: pure-Python pre-parsed SQL fixtures (zero sqlglot imports;
   loaded safely in parent BEFORE os.fork() so every CoW child reads fixtures in 0ms).
2. /app/tpc_tables/{tpcds,tpch}/<table>.pkl: pure-Python pre-parsed CSV rows for all
   TPC-DS (24) and TPC-H (8) tables (eliminates gzip + csv.reader + ast.literal_eval!).
3. /app/tpc_cache_py/{qi}.pkl (and /app/tpc_cache_c/{qi}.pkl if /sqlglot-c exists):
   per-query uncompressed baseline optimized ASTs + Plans so children only unpickle
   the exact queries they actually touch (~2ms/query vs ~950ms monolithic decompress!).
"""

from __future__ import annotations

import ast
import csv
import gzip
import pickle
import subprocess
import sys
import time
import zlib
from pathlib import Path


def _build_tpc_cache_for_root(sg_root: str, out_dir: str) -> int:
    code = f"""
import pickle, sys
from pathlib import Path
sys.path.insert(0, {sg_root!r})
from sqlglot import exp, parse_one
from sqlglot.optimizer import optimize
from sqlglot.planner import Plan
from sqlglot.schema import ensure_schema
import tests.helpers as th

def pack_expr(x):
    if isinstance(x, exp.Expression):
        return (
            "E",
            type(x),
            {{k: pack_expr(v) for k, v in x.args.items()}},
            pack_expr(x._type),
            x._meta,
            x.comments,
        )
    if isinstance(x, list):
        return [pack_expr(v) for v in x]
    if isinstance(x, tuple):
        return ("T", tuple(pack_expr(v) for v in x))
    return x

out = Path({out_dir!r})
out.mkdir(parents=True, exist_ok=True)
tpcds_sqls = list(th.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
s_ds = ensure_schema(th.TPCDS_SCHEMA)
cnt = 0
for i, (args, sql, _) in enumerate(tpcds_sqls, 1):
    if args.get("execute"):
        opt = optimize(parse_one(sql), s_ds, leave_tables_isolated=True)
        pl = Plan(opt.copy())
        (out / f"{{i}}.pkl").write_bytes(pickle.dumps((pack_expr(opt), pl), protocol=pickle.HIGHEST_PROTOCOL))
        cnt += 1
print(cnt)
"""
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return int(res.stdout.strip().splitlines()[-1])


def _build_tpc_tables(sg_root: Path) -> tuple[int, int]:
    sys.path.insert(0, str(sg_root))
    import tests.helpers as th

    total_files = 0
    total_bytes = 0
    for tag, sub, schema in [
        ("tpcds", "tests/fixtures/optimizer/tpc-ds/", th.TPCDS_SCHEMA),
        ("tpch", "tests/fixtures/optimizer/tpc-h/", th.TPCH_SCHEMA),
    ]:
        out_dir = Path("/app/tpc_tables") / tag
        out_dir.mkdir(parents=True, exist_ok=True)
        for table, col_types in schema.items():
            fn = sg_root / f"{sub}{table}.csv.gz"
            if not fn.exists():
                continue
            with open(fn, "rb") as f:
                is_gz = f.read(2) == b"\x1f\x8b"
            fh = gzip.open(fn, "rt", newline="") if is_gz else open(fn, encoding="utf-8", newline="")
            with fh:
                reader = csv.reader(fh, delimiter="|")
                next(reader)
                rows = []
                ctypes: list[type] = []
                for row in reader:
                    if not ctypes:
                        for v in row:
                            try:
                                ctypes.append(type(ast.literal_eval(v)))
                            except (ValueError, SyntaxError):
                                ctypes.append(str)
                    rows.append(
                        tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row))
                    )
            blob = pickle.dumps((col_types, rows), protocol=pickle.HIGHEST_PROTOCOL)
            (out_dir / f"{table}.pkl").write_bytes(blob)
            total_files += 1
            total_bytes += len(blob)
    return total_files, total_bytes


def main() -> None:
    t0 = time.perf_counter()
    sg_root = Path("/sqlglot")
    sys.path.insert(0, str(sg_root))
    import tests.helpers as th

    # 1. Pure-Python pre-parsed SQL fixtures
    fixtures_root = sg_root / "tests/fixtures"
    fixture_pairs: dict[str, list[tuple[dict, str, str]]] = {}
    fixture_singles: dict[str, list[str]] = {}
    for sql_file in sorted(fixtures_root.rglob("*.sql")):
        rel_under = str(sql_file.relative_to(fixtures_root))
        try:
            singles = list(th.load_sql_fixtures(rel_under))
            if singles:
                fixture_singles[rel_under] = singles
        except Exception:
            pass
        try:
            pairs = list(th.load_sql_fixture_pairs(rel_under))
            if pairs:
                fixture_pairs[rel_under] = pairs
        except Exception:
            pass

    fix_blob = zlib.compress(
        pickle.dumps(
            {"fixture_pairs": fixture_pairs, "fixture_singles": fixture_singles},
            protocol=pickle.HIGHEST_PROTOCOL,
        ),
        6,
    )
    Path("/app/fixture_cache.pkl.zlib").write_bytes(fix_blob)

    # 2. Pure-Python pre-parsed TPC-DS & TPC-H CSV tables
    n_tbl, tbl_bytes = _build_tpc_tables(sg_root)

    # 3. Per-query uncompressed TPC-DS AST & Plan caches for /sqlglot, /sqlglot-c, and /sqlglot-c-base
    n_py = _build_tpc_cache_for_root("/sqlglot", "/app/tpc_cache_py")
    n_c = 0
    if Path("/sqlglot-c/sqlglot").is_dir():
        n_c = _build_tpc_cache_for_root("/sqlglot-c", "/app/tpc_cache_c")
    n_cb = 0
    if Path("/sqlglot-c-base/sqlglot").is_dir():
        n_cb = _build_tpc_cache_for_root("/sqlglot-c-base", "/app/tpc_cache_cb")

    # 4. Precompute exact baseline PythonExecutor result (columns, rows) for all 57 TPC-DS queries
    #    so duckdb-only commits skip PythonExecutor completely (~300x faster)!
    rows_code = """
import pickle, sys
from pathlib import Path
sys.path.insert(0, "/sqlglot")
sys.path.insert(0, "/app")
import runner, candidate_engine as ce, tests.helpers as th
from sqlglot.executor.python import PythonExecutor
from sqlglot.executor.table import Table, ensure_tables

runner._install_fast_python_executor(ce.CommitPlan(commit="cache", is_noop=False))
tables = {}
for tname in th.TPCDS_SCHEMA:
    ct, rs = pickle.loads(Path(f"/app/tpc_tables/tpcds/{tname}.pkl").read_bytes())
    tables[tname] = Table(columns=ct, rows=list(rs))
out = Path("/app/tpc_rows")
out.mkdir(parents=True, exist_ok=True)
cnt = 0
for p in sorted(Path("/app/tpc_cache_py").glob("*.pkl")):
    qi = int(p.stem)
    _, plan = pickle.loads(p.read_bytes())
    tbl = PythonExecutor(tables=ensure_tables(tables)).execute(plan)
    (out / f"{qi}.pkl").write_bytes(pickle.dumps((tuple(tbl.columns), list(tbl.rows)), protocol=pickle.HIGHEST_PROTOCOL))
    cnt += 1
print(cnt)
"""
    res_r = subprocess.run([sys.executable, "-c", rows_code], capture_output=True, text=True, check=True)
    n_rows = int(res_r.stdout.strip().splitlines()[-1])

    print(
        f"[build_caches] fixtures={len(fixture_pairs)}p/{len(fixture_singles)}s ({len(fix_blob)/1024:.1f} KB), "
        f"tpc_tables={n_tbl} ({tbl_bytes/1024:.1f} KB), tpc_cache py={n_py} c={n_c} cb={n_cb} rows={n_rows} "
        f"in {time.perf_counter() - t0:.2f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
