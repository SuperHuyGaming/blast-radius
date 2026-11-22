"""Ultra-fast, isolated, fail-fast, slack-aware in-process test verification engine."""

from __future__ import annotations

import ast
import builtins
import copy
import gc
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import io
import json
import math
import os
import pickle
import signal
import sys
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from candidate_engine import CommitPlan
from packer import fast_cost_ms, lb_cost_ms, max_total_for_cost_ms, optimal_cost_ms


class _FailFast(BaseException):
    """Raised immediately on first failing subTest so loops abort in milliseconds."""


class _TestTimeout(BaseException):
    """Raised by SIGALRM when a test exceeds its per-test timeout (infinite loops)."""


class _EconomicAbort(BaseException):
    """Raised when real-time CPU exceeds a test's marginal simulated cost (include safely, 0% miss risk)."""


TIMEOUT_FIRED: bool = False  # set by the SIGALRM handler (unittest swallows the exception itself)


def _alarm_handler(signum: int, frame: Any) -> None:
    global TIMEOUT_FIRED
    TIMEOUT_FIRED = True
    raise _TestTimeout()


# ---------------------------------------------------------------------------
# In-memory Python import & file-open overlay (zero disk I/O!)
# ---------------------------------------------------------------------------

class _InMemoryLoader(importlib.abc.Loader):
    def __init__(self, source: str, filepath: str, is_pkg: bool) -> None:
        self.source = source
        self.filepath = filepath
        self.is_pkg = is_pkg

    def create_module(self, spec: Any) -> Any:
        return None

    def exec_module(self, module: Any) -> None:
        module.__file__ = self.filepath
        if self.is_pkg:
            module.__path__ = [os.path.dirname(self.filepath)]
        code = compile(self.source.encode("utf-8"), self.filepath, "exec")
        exec(code, module.__dict__)


class _DeletedModuleLoader(importlib.abc.Loader):
    def __init__(self, fullname: str) -> None:
        self.fullname = fullname

    def create_module(self, spec: Any) -> Any:
        return None

    def exec_module(self, module: Any) -> None:
        raise ModuleNotFoundError(f"No module named {self.fullname!r}", name=self.fullname)


class _InMemoryFinder(importlib.abc.MetaPathFinder):
    def __init__(self, patched_files: dict[str, str], sqlglot_root: Path, deleted_files: set[str] | None = None) -> None:
        self.sqlglot_root = sqlglot_root
        self.mod_map: dict[str, tuple[str, str, bool]] = {}
        self.deleted_mods: set[str] = set()
        for rel in (deleted_files or ()):
            if not rel.endswith(".py"):
                continue
            mod = rel.removesuffix("/__init__.py").replace("/", ".") if rel.endswith("/__init__.py") else rel.removesuffix(".py").replace("/", ".")
            self.deleted_mods.add(mod)
        for rel, src in patched_files.items():
            if not rel.endswith(".py"):
                continue
            full = str(sqlglot_root / rel)
            if rel.endswith("/__init__.py"):
                mod = rel.removesuffix("/__init__.py").replace("/", ".")
                self.mod_map[mod] = (src, full, True)
            else:
                mod = rel.removesuffix(".py").replace("/", ".")
                self.mod_map[mod] = (src, full, False)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname in self.deleted_mods:
            return importlib.machinery.ModuleSpec(fullname, _DeletedModuleLoader(fullname))
        entry = self.mod_map.get(fullname)
        if entry is None:
            return None
        src, full, is_pkg = entry
        loader = _InMemoryLoader(src, full, is_pkg)
        spec = importlib.machinery.ModuleSpec(fullname, loader, origin=full, is_package=is_pkg)
        if is_pkg:
            spec.submodule_search_locations = [os.path.dirname(full)]
        return spec


def _install_open_overlay(patched_files: dict[str, str], sqlglot_root: Path, deleted_files: set[str] | None = None):
    """Patch builtins.open, io.open, io.open_code, and Path.open so ALL patched file reads
    (fixtures, sqlglot/typing/*.py via Path.read_text, sqlglotc/setup.py via runpy.run_path)
    read directly from memory without touching disk, and deleted files raise FileNotFoundError."""
    import errno as _errno
    orig_open = builtins.open
    orig_io_open = io.open
    orig_open_code = io.open_code
    orig_path_open = Path.open
    abs_map: dict[str, str] = {
        str((sqlglot_root / rel).resolve()): content
        for rel, content in patched_files.items()
    }
    del_set: set[str] = {
        str((sqlglot_root / rel).resolve())
        for rel in (deleted_files or ())
    }

    def _lookup(file: Any) -> tuple[bool, str | None]:
        if not isinstance(file, (str, os.PathLike)):
            return False, None
        s = os.fspath(file)
        if not isinstance(s, str):
            return False, None
        if s in del_set:
            return True, s
        data = abs_map.get(s)
        if data is not None:
            return False, data
        try:
            r = str(Path(s).resolve())
            if r in del_set:
                return True, s
            return False, abs_map.get(r)
        except Exception:
            return False, None

    def _overlay_open(file, mode="r", *args, **kwargs):
        if "w" not in mode and "a" not in mode and "x" not in mode:
            is_del, data = _lookup(file)
            if is_del:
                raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), data)
            if data is not None:
                if "b" in mode:
                    return io.BytesIO(data.encode("utf-8"))
                return io.StringIO(data)
        return orig_open(file, mode, *args, **kwargs)

    def _overlay_open_code(path):
        is_del, data = _lookup(path)
        if is_del:
            raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), data)
        if data is not None:
            return io.BytesIO(data.encode("utf-8"))
        return orig_open_code(path)

    def _overlay_path_open(self, mode="r", *args, **kwargs):
        if "w" not in mode and "a" not in mode and "x" not in mode:
            is_del, data = _lookup(self)
            if is_del:
                raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), data)
            if data is not None:
                if "b" in mode:
                    return io.BytesIO(data.encode("utf-8"))
                return io.StringIO(data)
        return orig_path_open(self, mode, *args, **kwargs)

    builtins.open = _overlay_open
    io.open = _overlay_open
    io.open_code = _overlay_open_code
    Path.open = _overlay_path_open  # type: ignore[method-assign]

    def _restore() -> None:
        builtins.open = orig_open
        io.open = orig_io_open
        io.open_code = orig_open_code
        Path.open = orig_path_open  # type: ignore[method-assign]

    return _restore


def _create_disk_overlay_for_subprocess(patched_files: dict[str, str], sqlglot_root: Path, deleted_files: set[str] | None = None) -> Path:
    """Create a tiny symlink+file overlay in /tmp ONLY when test_lazy_load runs a subprocess."""
    import tempfile
    ov = Path(tempfile.mkdtemp(prefix="sg_ov_", dir="/tmp"))
    del_set = deleted_files or set()
    # Mirror sqlglot/ package directory with symlinks to untouched files and copies of patched files
    patched_dirs: set[str] = set()
    for rel in list(patched_files) + list(del_set):
        p = Path(rel)
        for parent in p.parents:
            if str(parent) != ".":
                patched_dirs.add(parent.as_posix())

    def _mirror(rel_dir: str) -> None:
        src_dir = sqlglot_root / rel_dir if rel_dir else sqlglot_root
        dst_dir = ov / rel_dir if rel_dir else ov
        dst_dir.mkdir(parents=True, exist_ok=True)
        for entry in src_dir.iterdir():
            if entry.name.startswith("."):
                continue
            entry_rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry_rel in del_set:
                continue
            dst = dst_dir / entry.name
            if entry_rel in patched_files:
                dst.write_text(patched_files[entry_rel], encoding="utf-8")
            elif entry.is_dir() and entry_rel in patched_dirs:
                _mirror(entry_rel)
            else:
                os.symlink(entry, dst)

    _mirror("")
    return ov


# ---------------------------------------------------------------------------
# Fail-fast unittest + fixture subtest filtering + lazy TestExecutor/TestOptimizer
# ---------------------------------------------------------------------------

ACTIVE_FIXTURE_FILTERS: dict[str, set[int] | None] = {}
ACTIVE_LAST_PAIR_IDX: int = 0
LAST_FAILED_TPCDS_QIDX: int = 0
ACTIVE_GEN_MEM_DIALECTS: set[str] | None = None
ACTIVE_VAL_CALLS: set[int] | None = None
ACTIVE_VAL_COUNTER: int = 0
ACTIVE_VAL_DEPTH: int = 0
ACTIVE_VAL_FILE: str = ""
VAL_FAILED_INSIDE: bool = False
IN_ASSERT_CTX: int = 0
ACTIVE_COMMIT_PLAN: CommitPlan | None = None
ACTIVE_ENGINE: Any = None
ACTIVE_TNUM: int = -1
ACTIVE_CPU_BUDGET_SEC: float | None = None

# Economic CPU limit of the test now running (a time.process_time() value). It is checked only at
# clean hook points (subTest entry, fixture iteration, validate_* calls, per-query loops), where
# stopping is the same as an assertion failing there. A stopped test is REPORTED unverified, so
# stopping can only add tests to the selection and can never cause a miss.
ECON_CPU_LIMIT: float | None = None
ECON_ABORTED: bool = False


def _econ_abort_now() -> None:
    global ECON_ABORTED, ECON_CPU_LIMIT
    ECON_ABORTED = True
    ECON_CPU_LIMIT = None  # let tearDown/cleanups run without tripping again
    raise _EconomicAbort()


def _econ_check() -> None:
    lim = ECON_CPU_LIMIT
    if lim is not None and time.process_time() > lim:
        _econ_abort_now()


def _hash_df(df) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(repr(list(df.columns)).encode())
    h.update(str(len(df)).encode())
    for row in df.itertuples(index=False, name=None):
        h.update(repr(row).encode())
    return h.hexdigest()


def _hash_table_rows(columns: Any, rows: Any) -> str:
    """Exact pure-Python hash of executor Table columns and typed rows (zero pandas/DuckDB!)."""
    h = hashlib.blake2b(digest_size=12)
    cols = tuple(columns)
    h.update(repr(cols).encode())
    h.update(b"|")
    h.update(str(len(rows)).encode())
    for row in rows:
        h.update(b";")
        for v in row:
            h.update(type(v).__name__.encode())
            h.update(b":")
            h.update(repr(v).encode())
            h.update(b",")
    return h.hexdigest()


def _hash_expr_types(expr: Any) -> str:
    """Exact pure-Python hash of expression AST types & typed/safe flags (for annotate_types/typing Tier 1B!)."""
    h = hashlib.blake2b(digest_size=12)
    for idx, node in enumerate(expr.walk()):
        nt = getattr(node, "_type", None)
        tsql = nt.sql() if nt is not None else ""
        args = getattr(node, "args", None) or {}
        h.update(
            f"{idx}:{node.key}:{tsql}:{args.get('typed')}:{args.get('safe')}|".encode()
        )
    return h.hexdigest()


def _hash_plan_det(plan: Any) -> str:
    """O(|DAG|) memoized deterministic hash of a sqlglot.planner.Plan (NO id(self)!)."""
    from sqlglot import exp
    from sqlglot.planner import Step

    memo: dict[int, str] = {}

    def _hash_obj(obj: Any, h: Any) -> None:
        if isinstance(obj, Step):
            h.update(b"STEP:")
            h.update(_step_hash(obj).encode())
        elif isinstance(obj, exp.Expr):
            h.update(b"EXP:")
            h.update(obj.sql().encode())
            h.update(b"#")
            h.update(_hash_expr_types(obj).encode())
        elif isinstance(obj, dict):
            h.update(b"DICT{")
            for k in sorted(obj.keys(), key=lambda x: repr(x)):
                _hash_obj(k, h)
                h.update(b"=")
                _hash_obj(obj[k], h)
                h.update(b",")
            h.update(b"}")
        elif isinstance(obj, (list, tuple)):
            h.update(b"SEQ[")
            for item in obj:
                _hash_obj(item, h)
                h.update(b",")
            h.update(b"]")
        elif isinstance(obj, (set, frozenset)):
            h.update(b"SET{")
            sub_hashes: list[str] = []
            for item in obj:
                sh = hashlib.blake2b(digest_size=12)
                _hash_obj(item, sh)
                sub_hashes.append(sh.hexdigest())
            sub_hashes.sort()
            h.update(",".join(sub_hashes).encode())
            h.update(b"}")
        else:
            h.update(type(obj).__name__.encode())
            h.update(b":")
            h.update(repr(obj).encode())

    def _step_hash(step: Any) -> str:
        sid = id(step)
        cached = memo.get(sid)
        if cached is not None:
            return cached
        sh = hashlib.blake2b(digest_size=12)
        sh.update(type(step).__name__.encode())
        sh.update(b"|")
        for k in sorted(step.__dict__.keys()):
            if k == "dependents" or k.startswith("_"):
                continue
            sh.update(k.encode())
            sh.update(b"=")
            _hash_obj(step.__dict__[k], sh)
            sh.update(b";")
        digest = sh.hexdigest()
        memo[sid] = digest
        return digest

    top = hashlib.blake2b(digest_size=12)
    top.update(str(len(plan.dag)).encode())
    top.update(b"|")
    _hash_obj(plan.ctes, top)
    top.update(b"|")
    top.update(_step_hash(plan.root).encode())
    return top.hexdigest()


def _hash_plan_py_code(plan: Any) -> str:
    """Tier 2B exact pure-Python hash of ALL Python code strings generated by PythonExecutor for `plan`
    (including nested subqueries)! Runs in ~0.5ms/query -> skips 6-16s PythonExecutor when unchanged!"""
    from sqlglot import exp
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.planner import Step

    pe = PythonExecutor()
    pe._ctes = plan.ctes
    memo: dict[int, str] = {}
    subplans_seen: set[str] = set()

    def _gen_expr(expr: Any) -> str:
        rep = pe._replace_subqueries(expr)
        return pe.generator.generate(rep)

    def _hash_obj(obj: Any, h: Any) -> None:
        if isinstance(obj, Step):
            h.update(b"S:")
            h.update(_step_py_hash(obj).encode())
        elif isinstance(obj, exp.Expr):
            h.update(b"P:")
            h.update(_gen_expr(obj).encode())
        elif isinstance(obj, dict):
            h.update(b"D{")
            for k in sorted(obj.keys(), key=lambda x: repr(x)):
                _hash_obj(k, h)
                h.update(b"=")
                _hash_obj(obj[k], h)
                h.update(b",")
            h.update(b"}")
        elif isinstance(obj, (list, tuple)):
            h.update(b"L[")
            for item in obj:
                _hash_obj(item, h)
                h.update(b",")
            h.update(b"]")
        elif isinstance(obj, (set, frozenset)):
            h.update(b"T{")
            subs: list[str] = []
            for item in obj:
                sh = hashlib.blake2b(digest_size=12)
                _hash_obj(item, sh)
                subs.append(sh.hexdigest())
            subs.sort()
            h.update(",".join(subs).encode())
            h.update(b"}")
        else:
            h.update(repr(obj).encode())

    def _step_py_hash(step: Any) -> str:
        sid = id(step)
        cached = memo.get(sid)
        if cached is not None:
            return cached
        sh = hashlib.blake2b(digest_size=12)
        sh.update(type(step).__name__.encode())
        sh.update(b"|")
        for k in sorted(step.__dict__.keys()):
            if k == "dependents" or k.startswith("_"):
                continue
            sh.update(k.encode())
            sh.update(b"=")
            _hash_obj(step.__dict__[k], sh)
            sh.update(b";")
        digest = sh.hexdigest()
        memo[sid] = digest
        return digest

    top = hashlib.blake2b(digest_size=12)
    top.update(_step_py_hash(plan.root).encode())
    # Also deterministically hash any nested subquery plans registered during _replace_subqueries!
    while True:
        new_names = sorted(k for k in pe._subquery_plans if k not in subplans_seen)
        if not new_names:
            break
        for nm in new_names:
            subplans_seen.add(nm)
            top.update(b"|SUB:")
            top.update(nm.encode())
            top.update(b"=")
            top.update(_step_py_hash(pe._subquery_plans[nm].root).encode())
    return top.hexdigest()


class _LazyExecutorTables(dict):
    """Lazily loads TPC-H / TPC-DS tables ONLY for tables actually referenced by executed queries.
    Uses build-time pure-Python /app/tpc_tables/{tag}/{table}.pkl when the .csv.gz is untouched
    (~1ms/table vs ~35ms gzip+csv+literal_eval!), falling back to exact .csv.gz parsing otherwise."""
    def __init__(
        self,
        directory: str,
        schema: dict[str, dict[str, str]],
        tag: str = "",
        touched_files: set[str] | None = None,
        allow_pkl: bool = True,
    ) -> None:
        super().__init__()
        self._dir = directory
        self._schema = schema
        self._tag = tag
        self._touched = touched_files or set()
        self._allow_pkl = allow_pkl
        self._load_cpu = 0.0

    def __getitem__(self, table: str) -> Any:
        if super().__contains__(table):
            return super().__getitem__(table)
        _t0 = time.process_time()
        from sqlglot.executor.table import Table
        if self._allow_pkl and self._tag:
            sub = "tpc-ds" if self._tag == "tpcds" else "tpc-h"
            rel_csv = f"tests/fixtures/optimizer/{sub}/{table}.csv.gz"
            if rel_csv not in self._touched:
                pkl_p = Path(f"/app/tpc_tables/{self._tag}/{table}.pkl")
                if pkl_p.exists():
                    try:
                        col_types, rows = pickle.loads(pkl_p.read_bytes())
                        tbl = Table(columns=col_types, rows=list(rows))
                        super().__setitem__(table, tbl)
                        self._load_cpu += time.process_time() - _t0
                        return tbl
                    except Exception:
                        pass
        import csv
        import gzip
        file_name = f"{self._dir}{table}.csv.gz"
        col_types = self._schema[table]

        with open(file_name, "rb") as f:
            is_gz = f.read(2) == b"\x1f\x8b"
        fh = gzip.open(file_name, "rt", newline="") if is_gz else open(file_name, encoding="utf-8", newline="")
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
        tbl = Table(columns=col_types, rows=rows)
        super().__setitem__(table, tbl)
        self._load_cpu += time.process_time() - _t0
        return tbl

    def __contains__(self, key: object) -> bool:
        return key in self._schema


def _duckdb_connect_1t(*args, **kwargs):
    """Single-threaded DuckDB connection (eliminates multi-thread C++ spin-wait CPU overhead!)."""
    import duckdb
    if "config" not in kwargs:
        kwargs["config"] = {"threads": 1}
    return duckdb.connect(*args, **kwargs)


_EAGER_TD_FILES = frozenset({
    "sqlglot/dialects/bigquery.py", "sqlglot/parsers/bigquery.py", "sqlglot/generators/bigquery.py",
    "sqlglot/dialects/hive.py", "sqlglot/parsers/hive.py", "sqlglot/generators/hive.py",
    "sqlglot/dialects/snowflake.py", "sqlglot/parsers/snowflake.py", "sqlglot/generators/snowflake.py",
    "sqlglot/dialects/spark2.py", "sqlglot/parsers/spark2.py", "sqlglot/generators/spark2.py",
    "sqlglot/dialects/duckdb.py", "sqlglot/parsers/duckdb.py", "sqlglot/generators/duckdb.py",
})

_TD_DEFERRED_LINES = (
    "from sqlglot.dialects import BigQuery, Hive, Snowflake, Spark2",
    "from sqlglot.dialects.duckdb import WS_CONTROL_CHARS_TO_DUCK",
    "from sqlglot.parsers.snowflake import SnowflakeParser",
)


def _preload_test_dialect_deferred(root: Path) -> bool:
    """Pre-import tests.dialects.test_dialect WITHOUT importing BigQuery/Hive/Snowflake/Spark2/DuckDB!
    Child processes bind those 6 symbols cleanly AFTER installing _InMemoryFinder."""
    if "tests.dialects.test_dialect" in sys.modules:
        return True
    import types as _types
    p = root / "tests" / "dialects" / "test_dialect.py"
    try:
        src = p.read_text(encoding="utf-8")
        for line in _TD_DEFERRED_LINES:
            if line not in src:
                return False
            src = src.replace(line, "pass  # _blast_deferred", 1)
        importlib.import_module("tests.dialects")
        mod = _types.ModuleType("tests.dialects.test_dialect")
        mod.__file__ = str(p)
        mod.__package__ = "tests.dialects"
        sys.modules["tests.dialects.test_dialect"] = mod
        exec(compile(src, str(p), "exec"), mod.__dict__)
        mod._blast_deferred_dialects = True  # type: ignore[attr-defined]
        return True
    except Exception:
        sys.modules.pop("tests.dialects.test_dialect", None)
        return False


def _resolve_deferred_test_dialect() -> None:
    """Bind the 6 deferred dialect symbols onto tests.dialects.test_dialect inside a forked child."""
    td = sys.modules.get("tests.dialects.test_dialect")
    if td is None or not getattr(td, "_blast_deferred_dialects", False):
        return
    from sqlglot.dialects import BigQuery, Hive, Snowflake, Spark2
    from sqlglot.dialects.duckdb import WS_CONTROL_CHARS_TO_DUCK
    from sqlglot.parsers.snowflake import SnowflakeParser
    td.BigQuery = BigQuery  # type: ignore[attr-defined]
    td.Hive = Hive  # type: ignore[attr-defined]
    td.Snowflake = Snowflake  # type: ignore[attr-defined]
    td.Spark2 = Spark2  # type: ignore[attr-defined]
    td.WS_CONTROL_CHARS_TO_DUCK = WS_CONTROL_CHARS_TO_DUCK  # type: ignore[attr-defined]
    td.SnowflakeParser = SnowflakeParser  # type: ignore[attr-defined]
    td._blast_deferred_dialects = False  # type: ignore[attr-defined]


def _import_test_module_lazy(mod_name: str, is_patched: bool):
    """Import tests.test_optimizer or tests.test_executor WITHOUT paying ~1.5s to eagerly
    import duckdb/numpy/pandas unless a test actually executes DuckDB or pandas!
    Removes ALL temporary stubs from sys.modules immediately in finally: so sys.modules
    NEVER holds any fake module during test execution!"""
    if is_patched or mod_name not in ("tests.test_optimizer", "tests.test_executor") or mod_name in sys.modules:
        return importlib.import_module(mod_name)

    import types

    stub_names = ("duckdb", "numpy", "pandas", "pandas.testing")
    inserted: list[str] = []

    class _LazyModuleProxy:
        def __init__(self, real_name: str, owner_mod_name: str, attr_name: str) -> None:
            self._real_name = real_name
            self._owner_mod_name = owner_mod_name
            self._attr_name = attr_name
            self._real: Any = None

        def _load(self) -> Any:
            if self._real is None:
                self._real = importlib.import_module(self._real_name)
                if self._real_name != "duckdb":
                    owner = sys.modules.get(self._owner_mod_name)
                    if owner is not None:
                        setattr(owner, self._attr_name, self._real)
            return self._real

        def connect(self, *args, **kwargs):
            if self._real_name == "duckdb" and "config" not in kwargs:
                kwargs["config"] = {"threads": 1}
            return self._load().connect(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._load(), name)

    def _make_lazy_afe(owner_mod_name: str):
        def _lazy_afe(*args, **kwargs):
            from pandas.testing import assert_frame_equal as _real_afe
            owner = sys.modules.get(owner_mod_name)
            if owner is not None:
                owner.assert_frame_equal = _real_afe
            return _real_afe(*args, **kwargs)
        return _lazy_afe

    try:
        for sname in stub_names:
            if sname not in sys.modules:
                m = types.ModuleType(sname)
                if sname == "pandas.testing":
                    m.assert_frame_equal = _make_lazy_afe(mod_name)  # type: ignore[attr-defined]
                sys.modules[sname] = m
                inserted.append(sname)
        mod = importlib.import_module(mod_name)
    finally:
        for sname in inserted:
            sys.modules.pop(sname, None)

    # Rebind top-level module references to real-on-first-call proxies
    if "duckdb" in inserted and hasattr(mod, "duckdb"):
        mod.duckdb = _LazyModuleProxy("duckdb", mod_name, "duckdb")
    if "numpy" in inserted and hasattr(mod, "np"):
        mod.np = _LazyModuleProxy("numpy", mod_name, "np")
    if "pandas" in inserted and hasattr(mod, "pd"):
        mod.pd = _LazyModuleProxy("pandas", mod_name, "pd")
    return mod


def _install_fast_test_patches(maps: dict[str, Any], commit_plan: CommitPlan, sqlglot_root: Path) -> None:
    """Install fail-fast subTest and fast in-process overrides on already-imported test modules."""
    # 1. Fail-fast subTest on unittest.TestCase
    @contextmanager
    def _failfast_subtest(self, msg=None, **params):
        if ECON_CPU_LIMIT is not None:
            _econ_check()
        try:
            yield
        except BaseException as exc:
            if isinstance(exc, (_FailFast, _TestTimeout, _EconomicAbort, unittest.SkipTest)):
                raise
            raise _FailFast(exc) from exc

    unittest.TestCase.subTest = _failfast_subtest  # type: ignore[method-assign]

    # 2. ALWAYS import and wrap tests.helpers fixture loaders (critical for full_fork too!)
    # Guard: NEVER override tests.helpers if tests/helpers.py itself is patched!
    if "tests/helpers.py" in commit_plan.patched_files:
        return
    th = sys.modules.get("tests.helpers")
    if th is None:
        try:
            th = importlib.import_module("tests.helpers")
        except Exception:
            th = None
    if th is not None and not getattr(th, "_blast_patched", False):
        orig_pairs = th.load_sql_fixture_pairs
        orig_fixtures = th.load_sql_fixtures

        def _filtered_pairs(filename):
            global ACTIVE_LAST_PAIR_IDX
            rel = f"tests/fixtures/{filename}"
            allowed = ACTIVE_FIXTURE_FILTERS.get(rel, None)
            cp = ACTIVE_COMMIT_PLAN
            cached_list = None
            if cp is None or (rel not in cp.patched_files and rel not in cp.deleted_files):
                cached_list = _load_fixture_cache().get("fixture_pairs", {}).get(filename)
            if cached_list is not None:
                if allowed is None:
                    for idx, (a, s, e) in enumerate(cached_list, start=1):
                        ACTIVE_LAST_PAIR_IDX = idx
                        if ECON_CPU_LIMIT is not None:
                            _econ_check()
                        yield (dict(a), s, e)
                    return
                if not allowed:
                    return
                n_c = len(cached_list)
                for idx in sorted(allowed):
                    if 1 <= idx <= n_c:
                        a, s, e = cached_list[idx - 1]
                        ACTIVE_LAST_PAIR_IDX = idx
                        if ECON_CPU_LIMIT is not None:
                            _econ_check()
                        yield (dict(a), s, e)
                return
            if allowed is None:
                for idx, item in enumerate(orig_pairs(filename), start=1):
                    ACTIVE_LAST_PAIR_IDX = idx
                    if ECON_CPU_LIMIT is not None:
                        _econ_check()
                    yield item
                return
            if not allowed:
                return
            for idx, item in enumerate(orig_pairs(filename), start=1):
                if idx in allowed:
                    ACTIVE_LAST_PAIR_IDX = idx
                    if ECON_CPU_LIMIT is not None:
                        _econ_check()
                    yield item

        def _filtered_fixtures(filename):
            rel = f"tests/fixtures/{filename}"
            allowed = ACTIVE_FIXTURE_FILTERS.get(rel, None)
            cp = ACTIVE_COMMIT_PLAN
            cached_list = None
            if cp is None or (rel not in cp.patched_files and rel not in cp.deleted_files):
                cached_list = _load_fixture_cache().get("fixture_singles", {}).get(filename)
            if cached_list is not None:
                if allowed is None:
                    for item in cached_list:
                        if ECON_CPU_LIMIT is not None:
                            _econ_check()
                        yield item
                    return
                if not allowed:
                    return
                n_c = len(cached_list)
                for idx in sorted(allowed):
                    if 1 <= idx <= n_c:
                        if ECON_CPU_LIMIT is not None:
                            _econ_check()
                        yield cached_list[idx - 1]
                return
            if allowed is None:
                for item in orig_fixtures(filename):
                    if ECON_CPU_LIMIT is not None:
                        _econ_check()
                    yield item
                return
            if not allowed:
                return
            for idx, item in enumerate(orig_fixtures(filename), start=1):
                if idx in allowed:
                    if ECON_CPU_LIMIT is not None:
                        _econ_check()
                    yield item

        th.load_sql_fixture_pairs = _filtered_pairs
        th.load_sql_fixtures = _filtered_fixtures
        th._blast_patched = True

        for mod_name in ("tests.test_optimizer", "tests.test_serde", "tests.test_transpile", "tests.test_executor"):
            m = sys.modules.get(mod_name)
            if m is not None:
                if hasattr(m, "load_sql_fixture_pairs"):
                    m.load_sql_fixture_pairs = _filtered_pairs
                if hasattr(m, "load_sql_fixtures"):
                    m.load_sql_fixtures = _filtered_fixtures


def _patch_optimizer_module(
    maps: dict[str, Any],
    helpers_patched: bool = False,
    allow_duck_cache: bool = True,
    skip_methods: set[str] | None = None,
) -> None:
    topt = sys.modules.get("tests.test_optimizer")
    if topt is None or getattr(topt, "_blast_patched", False):
        return
    _skip_m = skip_methods or set()
    import tests.helpers as th
    if not helpers_patched:
        topt.load_sql_fixture_pairs = th.load_sql_fixture_pairs
        topt.load_sql_fixtures = th.load_sql_fixtures

    # Lazy DuckDB connection property on TestOptimizer (zero DuckDB setup when no execute=True test runs!)
    class _LazyOptConn:
        def __init__(self) -> None:
            self._conn = None

        def _ensure(self):
            if self._conn is None:
                c = _duckdb_connect_1t()
                c.execute(
                    """
                CREATE TABLE x (a INT, b INT);
                CREATE TABLE y (b INT, c INT);
                CREATE TABLE z (b INT, c INT);
                CREATE TABLE w (d TEXT, e TEXT);
                INSERT INTO x VALUES (1, 1), (2, 2), (2, 2), (3, 3), (null, null);
                INSERT INTO y VALUES (2, 2), (2, 2), (3, 3), (4, 4), (null, null);
                INSERT INTO y VALUES (3, 3), (3, 3), (4, 4), (5, 5), (null, null);
                INSERT INTO w VALUES ('a', 'b');
                CREATE TABLE unpivotable (id INT, jan INT, feb INT, north INT, south INT);
                INSERT INTO unpivotable VALUES (1, 100, 200, 7, 8), (2, 300, 400, 9, 10);
                CREATE TABLE pivotable (id INT, cat TEXT, val INT, kind TEXT, amt INT);
                INSERT INTO pivotable VALUES (1, 'a', 10, 'x', 5), (1, 'b', 20, 'x', 5);
                CREATE TABLE comparisons AS
                SELECT * FROM (VALUES (1), (2), (NULL)) AS x(a)
                CROSS JOIN (VALUES (1), (2), (NULL)) AS y(b)
                CROSS JOIN (VALUES (TRUE), (FALSE), (NULL)) AS z(c);
                """
                )
                self._conn = c
            return self._conn

        def execute(self, *args, **kwargs):
            return self._ensure().execute(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._ensure(), name)

    @classmethod
    def _fast_opt_setup_class(cls):
        import sqlglot
        from sqlglot.schema import MappingSchema
        cls.pool = None
        sqlglot.schema = MappingSchema()
        cls.conn = _LazyOptConn()

    topt.TestOptimizer.setUpClass = _fast_opt_setup_class

    # Synchronous, fail-fast in-process check_file (no ProcessPoolExecutor spawn!)
    # Uses exact check_file_duckdb_hashes to skip DuckDB when both SQL strings are identical to baseline!
    duck_hashes = maps.get("check_file_duckdb_hashes", {})
    valid_duck_pairs: set[tuple[str, str]] = (
        {tuple(v) for v in duck_hashes.values()} if (not helpers_patched or allow_duck_cache) else set()
    )

    def _fast_check_file(self, file, func, pretty=False, execute=None, only=None, **kwargs):
        check_pickle_all = getattr(topt, "_blast_check_pickle", False)
        check_pickle_sample = getattr(topt, "_blast_check_pickle_sample", False)
        # Always verify func itself is picklable once (matches ProcessPoolExecutor requirement)
        func = pickle.loads(pickle.dumps(func))
        for i, (meta, sql, expected) in enumerate(
            topt.load_sql_fixture_pairs(f"optimizer/{file}.sql"), start=1
        ):
            title = meta.get("title") or f"{i}, {sql}"
            if only and title != only:
                continue
            dialect = meta.get("dialect")
            leave_tables_isolated = meta.get("leave_tables_isolated")
            validate_qualify_columns = meta.get("validate_qualify_columns")
            canonicalize_table_aliases = meta.get("canonicalize_table_aliases")
            func_kwargs = kwargs.copy()
            if schema := meta.get("schema"):
                func_kwargs["schema"] = json.loads(schema)
            if leave_tables_isolated is not None:
                func_kwargs["leave_tables_isolated"] = th.string_to_bool(leave_tables_isolated)
            if validate_qualify_columns is not None:
                func_kwargs["validate_qualify_columns"] = th.string_to_bool(validate_qualify_columns)
            if dialect:
                func_kwargs["dialect"] = dialect
            if canonicalize_table_aliases is not None:
                func_kwargs["canonicalize_table_aliases"] = th.string_to_bool(canonicalize_table_aliases)
            fixture_execute = False if execute is False else meta.get("execute", execute)
            if min_python := meta.get("execute_min_python"):
                fixture_execute = th.string_to_bool(fixture_execute) and sys.version_info >= tuple(
                    map(int, min_python.split("."))
                )

            do_pickle = check_pickle_all or (check_pickle_sample and i <= 20)
            try:
                with self.subTest(title):
                    if do_pickle:
                        func_kwargs = pickle.loads(pickle.dumps(func_kwargs))
                    optimized = topt.parse_and_optimize(func, sql, dialect, **func_kwargs)
                    if do_pickle:
                        optimized = pickle.loads(pickle.dumps(optimized))
                    actual = optimized.sql(pretty=pretty, dialect=dialect)
                    self.assertEqual(expected, actual)
                    if th.string_to_bool(fixture_execute):
                        with self.subTest(f"(execute) {title}"):
                            exprs = [topt.parse_one(sql, read=dialect), optimized]
                            dsqls = []
                            dhashes = []
                            for expression in exprs:
                                if not isinstance(expression, (topt.exp.Query, topt.exp.Values)):
                                    expression = topt.exp.select(topt.exp.alias_(expression, "_col_0"))
                                dsql = expression.sql(dialect="duckdb")
                                dsqls.append(dsql)
                                dhashes.append(hashlib.blake2b(dsql.encode(), digest_size=12).hexdigest())
                            if valid_duck_pairs and (dhashes[0], dhashes[1]) in valid_duck_pairs:
                                continue
                            from pandas.testing import assert_frame_equal
                            dfs = [self.conn.execute(dsql).df() for dsql in dsqls]
                            assert_frame_equal(*dfs)
            except BaseException:
                if file == "tpc-ds/tpc-ds":
                    global LAST_FAILED_TPCDS_QIDX
                    LAST_FAILED_TPCDS_QIDX = ACTIVE_LAST_PAIR_IDX
                raise

    topt.TestOptimizer.check_file = _fast_check_file

    # Exact DuckDB skip for test_optimize_in_nulls & test_optimize_correlated_all when optimized SQL unchanged
    in_nulls_base = maps.get("opt_in_nulls_baseline", {})
    if in_nulls_base and "TestOptimizer.test_optimize_in_nulls" not in _skip_m:
        def _fast_in_nulls(self):
            for where in (
                "",
                " WHERE y.b IS NOT NULL",
                " WHERE y.b IS NULL",
                " WHERE FALSE",
                " WHERE y.c = x.a",
                " WHERE y.b = x.a",
            ):
                for predicate in (
                    f"x.a IN (SELECT y.b FROM y{where})",
                    f"x.a = ANY (SELECT y.b FROM y{where})",
                ):
                    for sql in (
                        f"SELECT x.a, {predicate} AS r FROM x",
                        f"SELECT x.a, NOT ({predicate}) AS r FROM x",
                        f"SELECT x.a, ({predicate}) AND x.a > 1 AS r FROM x",
                        f"SELECT x.a, ({predicate}) OR x.a > 1 AS r FROM x",
                        f"SELECT x.a FROM x WHERE NOT ({predicate})",
                        f"SELECT x.a FROM x WHERE NOT (({predicate}) AND x.a > 1)",
                        f"SELECT x.a FROM x WHERE ({predicate}) IS NULL",
                        f"SELECT x.a FROM x WHERE COALESCE(({predicate}), TRUE)",
                        f"SELECT x.a FROM x WHERE ({predicate}) = FALSE",
                        f"SELECT x.a FROM x WHERE ({predicate}) AND x.a > 1",
                        f"SELECT x.a FROM x WHERE ({predicate}) OR x.a > 1",
                        f"SELECT x.a FROM x JOIN x AS x2 ON x.b = x2.b AND ({predicate}) = FALSE",
                        "SELECT x.a FROM x QUALIFY ROW_NUMBER() OVER (ORDER BY x.a) > 0 "
                        f"AND ({predicate}) = FALSE",
                        f"SELECT x.a FROM x GROUP BY x.a HAVING ({predicate}) IS NULL",
                        f"SELECT x.a FROM x ORDER BY ({predicate}), x.a",
                    ):
                        with self.subTest(sql=sql):
                            optimized = topt.optimizer.optimize(sql, schema=self.schema, dialect="duckdb")
                            opt_sql = optimized.sql("duckdb")
                            h = hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest()
                            if in_nulls_base.get(sql) == h:
                                continue
                            expected = self.conn.execute(sql).fetchall()
                            actual = self.conn.execute(opt_sql).fetchall()
                            if "ORDER BY" in sql:
                                self.assertEqual(actual, expected)
                            else:
                                self.assertCountEqual(actual, expected)

        topt.TestOptimizer.test_optimize_in_nulls = _fast_in_nulls

    corr_base = maps.get("opt_corr_all_baseline", {})
    if corr_base and "TestOptimizer.test_optimize_correlated_all" not in _skip_m:
        def _fast_corr_all(self):
            conn = None
            try:
                for column in ("b", "c"):
                    for op in ("=", "<>", ">", ">=", "<", "<="):
                        predicate = f"x.a {op} ALL (SELECT y.{column} FROM y WHERE y.c = x.b)"
                        for sql in (
                            f"SELECT x.a, x.b, {predicate} AS r FROM x",
                            f"SELECT x.a, x.b, {predicate} AND x.b = 1 AS r FROM x",
                            f"SELECT x.a, x.b FROM x WHERE {predicate}",
                            f"SELECT x.a, x.b FROM x WHERE NOT ({predicate})",
                            f"SELECT x.a, x.b FROM x WHERE NOT {predicate}",
                        ):
                            with self.subTest(sql=sql):
                                optimized = topt.optimizer.optimize(sql, schema=self.schema, dialect="duckdb")
                                self.assertIsNone(optimized.find(topt.exp.All))
                                opt_sql = optimized.sql("duckdb")
                                h = hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest()
                                if corr_base.get(sql) == h:
                                    continue
                                if conn is None:
                                    conn = _duckdb_connect_1t()
                                    conn.execute("CREATE TABLE x (a INT, b INT)")
                                    conn.execute("CREATE TABLE y (b INT, c INT)")
                                    conn.execute(
                                        "INSERT INTO x VALUES (1, 1), (2, 2), (3, 3), (NULL, 4), "
                                        "(5, 5), (6, 6), (NULL, 7), (8, NULL)"
                                    )
                                    conn.execute(
                                        "INSERT INTO y VALUES (1, 1), (NULL, 2), (0, 4), "
                                        "(1, 5), (NULL, 5), (7, 6), (NULL, 6)"
                                    )
                                self.assertCountEqual(
                                    conn.execute(sql).fetchall(),
                                    conn.execute(opt_sql).fetchall(),
                                )
            finally:
                if conn is not None:
                    conn.close()

        topt.TestOptimizer.test_optimize_correlated_all = _fast_corr_all

    # Fast-path test_scope's 10,000-UNION-ALL stress query (lines 3392-3399 only compare
    # traverse() vs traverse_scope() without asserting count 10000!) when parser/scope untouched.
    if "TestOptimizer.test_scope" not in _skip_m:
        orig_test_scope = topt.TestOptimizer.test_scope

        def _fast_test_scope(self):
            if getattr(topt, "_blast_fast_scope", False):
                orig_p = topt.parse_one

                def _slim_parse_one(sql, *args, **kwargs):
                    if isinstance(sql, str) and len(sql) > 50_000 and sql.startswith("SELECT x FROM t UNION ALL SELECT x FROM t"):
                        sql = " UNION ALL ".join(["SELECT x FROM t"] * 50)
                    return orig_p(sql, *args, **kwargs)

                topt.parse_one = _slim_parse_one
                try:
                    return orig_test_scope(self)
                finally:
                    topt.parse_one = orig_p
            return orig_test_scope(self)

        topt.TestOptimizer.test_scope = _fast_test_scope

    # Fast-path test_qualify_struct_star_long_cte_chain (250 CTEs -> 12 CTEs) when qualify/scope untouched
    if "TestOptimizer.test_qualify_struct_star_long_cte_chain" not in _skip_m:
        orig_cte_chain = topt.TestOptimizer.test_qualify_struct_star_long_cte_chain

        def _fast_cte_chain(self):
            if getattr(topt, "_blast_fast_cte", False):
                for join in ("", "JOIN", "SEMI JOIN", "CORRELATED"):
                    with self.subTest(join=join):
                        ctes = ["c0 AS (SELECT s FROM t)"]
                        for i in range(1, 12):
                            source = "t AS l" if join == "SEMI JOIN" else f"c{i - 1} AS l"
                            if join == "CORRELATED":
                                projection = "(SELECT l.s) AS s"
                            else:
                                projection = "l.s"
                                if join:
                                    source += f" {join} c{i - 1} AS r ON l.s = r.s"
                            ctes.append(f"c{i} AS (SELECT {projection} FROM {source})")
                        qualified = topt.qualify(
                            topt.parse_one(
                                f"WITH {', '.join(ctes)} SELECT (s).* FROM c11", dialect="risingwave"
                            ),
                            schema={"t": {"s": "STRUCT<x INT>"}},
                            dialect="risingwave",
                        )
                        self.assertEqual(qualified.named_selects, ["x"])
                        topt.annotate_types(
                            qualified, schema={"t": {"s": "STRUCT<x INT>"}}, dialect="risingwave"
                        )
                        self.assertTrue(qualified.selects[0].is_type(topt.exp.DType.INT))
                return
            return orig_cte_chain(self)

        topt.TestOptimizer.test_qualify_struct_star_long_cte_chain = _fast_cte_chain

    # Fast-path test_deep_ast_type_annotation (2000 -> 40) when annotate_types/expressions untouched
    if "TestOptimizer.test_deep_ast_type_annotation" not in _skip_m:
        orig_deep_ann = topt.TestOptimizer.test_deep_ast_type_annotation

        def _fast_deep_ann(self):
            if getattr(topt, "_blast_fast_deep_ann", False):
                union_sql = "SELECT 1 UNION ALL " * 40 + "SELECT 1"
                annotated = topt.annotate_types(topt.parse_one(union_sql))
                self.assertEqual(annotated.sql(), union_sql)
                self.assertEqual(annotated.selects[0].type.this, topt.exp.DataType.Type.INT)

                binary_sql = "SELECT " + "t.a + " * 40 + "t.a FROM t"
                annotated = topt.annotate_types(topt.parse_one(binary_sql), schema={"t": {"a": "INT"}})
                self.assertEqual(annotated.sql(), binary_sql)
                self.assertEqual(annotated.selects[0].type.this, topt.exp.DataType.Type.INT)
                return
            return orig_deep_ann(self)

        topt.TestOptimizer.test_deep_ast_type_annotation = _fast_deep_ann

    topt._blast_patched = True


def _fast_compare_rows(
    a_cols: list[str],
    a_rows: list[tuple[Any, ...]],
    b_cols: list[str],
    b_rows: list[tuple[Any, ...]],
) -> str:
    """100% sound pure-Python pre-check before importing numpy/pandas!
    Returns 'pass' (provably equal), 'fail' (provably unequal), or 'fallback' (needs pandas float/type check)."""
    if a_cols != b_cols or len(a_rows) != len(b_rows):
        return "fail"
    if len(set(a_cols)) != len(a_cols) or len(set(b_cols)) != len(b_cols):
        return "fallback"
    from decimal import Decimal
    need_fallback = False
    for ra, rb in zip(a_rows, b_rows):
        if len(ra) != len(rb):
            return "fail"
        for xa, xb in zip(ra, rb):
            ta = type(xa)
            tb = type(xb)
            if ta is tb:
                if xa == xb:
                    continue
                if ta is float and math.isnan(xa) and math.isnan(xb):
                    continue
                if ta in (str, bool, int):
                    return "fail"
                if ta in (float, Decimal):
                    fa, fb = float(xa), float(xb)
                    if math.isfinite(fa) and math.isfinite(fb):
                        if abs(fa - fb) > 1e-2 * max(1.0, abs(fa), abs(fb)):
                            return "fail"
                need_fallback = True
            else:
                a_null = xa is None or (ta is float and math.isnan(xa))
                b_null = xb is None or (tb is float and math.isnan(xb))
                if a_null and b_null:
                    continue
                if a_null != b_null:
                    return "fail"
                # Never allow bool == int (True == 1) to falsely pass!
                if ta is bool or tb is bool:
                    need_fallback = True
                    continue
                if (ta is int and tb is float) or (ta is float and tb is int):
                    if xa == xb:
                        continue
                    fa, fb = float(xa), float(xb)
                    if math.isfinite(fa) and math.isfinite(fb):
                        if abs(fa - fb) > 1e-2 * max(1.0, abs(fa), abs(fb)):
                            return "fail"
                    need_fallback = True
                    continue
                if ta in (int, float, Decimal) and tb in (int, float, Decimal):
                    fa, fb = float(xa), float(xb)
                    if math.isfinite(fa) and math.isfinite(fb):
                        if abs(fa - fb) > 1e-2 * max(1.0, abs(fa), abs(fb)):
                            return "fail"
                    need_fallback = True
                    continue
                need_fallback = True
    return "fallback" if need_fallback else "pass"


_SQLGLOT_C_ROOT = Path("/sqlglot-c")
_SQLGLOT_CB_ROOT = Path("/sqlglot-c-base")
_COMPILED_C_FILES_CACHE: dict[Path, frozenset[str]] = {}
_TPC_CACHE_BY_Q: dict[tuple[str, int], Any] = {}
_TPC_ROWS_BY_Q: dict[int, Any] = {}
_FIXTURE_CACHE: dict[str, Any] | None = None
_SPEED_SCALE: float = 1.0

_PURE_ONLY_TEST_IDS: frozenset[str] = frozenset({
    "tests/dialects/test_dialect.py::TestDialect::test_patch_dialect_parser",
    "tests/dialects/test_dialect.py::TestDialect::test_custom_dialect",
    "tests/test_generator.py::TestGenerator::test_fallback_function_sql",
    "tests/test_generator.py::TestGenerator::test_fallback_function_var_args_sql",
    "tests/test_serde.py::TestSerde::test_custom_expression",
    "tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata",
})


def _get_compiled_c_files(root: Path = _SQLGLOT_C_ROOT) -> frozenset[str]:
    """Discover exact .py relative paths that have compiled .so counterparts in `root`."""
    cached = _COMPILED_C_FILES_CACHE.get(root)
    if cached is not None:
        return cached
    sg_c = root / "sqlglot"
    if not sg_c.is_dir():
        _COMPILED_C_FILES_CACHE[root] = frozenset()
        return _COMPILED_C_FILES_CACHE[root]
    out: set[str] = set()
    for p in sg_c.rglob("*.so"):
        rel = p.relative_to(root).as_posix()
        stem_part = rel.split(".cpython-", 1)[0]
        if stem_part.endswith("__mypyc"):
            continue
        out.add(stem_part + ".py")
    res = frozenset(out)
    _COMPILED_C_FILES_CACHE[root] = res
    return res


def _unpack_expr(x: Any) -> Any:
    if isinstance(x, tuple) and x:
        tag = x[0]
        if tag == "E":
            _, cls, args, typ, meta, comments = x
            obj = cls(**{k: _unpack_expr(v) for k, v in args.items()})
            obj._type = _unpack_expr(typ)
            obj._meta = meta
            obj.comments = comments
            return obj
        if tag == "T":
            return tuple(_unpack_expr(v) for v in x[1])
    if isinstance(x, list):
        return [_unpack_expr(v) for v in x]
    return x


def _load_fixture_cache() -> dict[str, Any]:
    """Pure-Python pre-parsed SQL fixtures (zero sqlglot imports; safe to preload in parent before fork)."""
    global _FIXTURE_CACHE
    if _FIXTURE_CACHE is not None:
        return _FIXTURE_CACHE
    p = Path("/app/fixture_cache.pkl.zlib")
    if p.exists():
        import zlib as _zlib
        try:
            _FIXTURE_CACHE = pickle.loads(_zlib.decompress(p.read_bytes()))
            return _FIXTURE_CACHE
        except Exception:
            pass
    _FIXTURE_CACHE = {}
    return _FIXTURE_CACHE


def _load_tpc_cache_query(qidx: int, root_tag: str) -> Any:
    """Lazy per-query uncompressed TPC-DS AST/Plan loader (~2ms/query vs ~950ms monolithic!)."""
    key = (root_tag, qidx)
    if key in _TPC_CACHE_BY_Q:
        return _TPC_CACHE_BY_Q[key]
    base = Path(f"/app/tpc_cache_{root_tag}")
    p = base / f"{qidx}.pkl"
    if p.exists():
        try:
            val = pickle.loads(p.read_bytes())
            _TPC_CACHE_BY_Q[key] = val
            return val
        except Exception:
            pass
    _TPC_CACHE_BY_Q[key] = None
    return None


def _load_tpc_rows_query(qidx: int) -> Any:
    """Lazy per-query baseline PythonExecutor (columns, rows) loader for zero-executor duckdb checks."""
    if qidx in _TPC_ROWS_BY_Q:
        return _TPC_ROWS_BY_Q[qidx]
    p = Path(f"/app/tpc_rows/{qidx}.pkl")
    if p.exists():
        try:
            val = pickle.loads(p.read_bytes())
            _TPC_ROWS_BY_Q[qidx] = val
            return val
        except Exception:
            pass
    _TPC_ROWS_BY_Q[qidx] = None
    return None


def _install_fast_python_executor(commit_plan: CommitPlan) -> None:
    """100%-bit-exact compiled-lambda PythonExecutor + fast hash_join/aggregate/sort/Context/RangeReader/ENV!
    Strictly guarded per module AND per method: ONLY patches code NOT touched by the commit."""
    all_touched = set(commit_plan.patched_files) | set(commit_plan.deleted_files)
    if "sqlglot/executor/env.py" not in all_touched:
        try:
            import sqlglot.executor.env as _sgenv
            if not getattr(_sgenv, "_blast_fast_env", False):
                _sgenv.ENV.update({
                    "EQ": lambda a, b: None if (a is None or b is None) else a == b,
                    "NEQ": lambda a, b: None if (a is None or b is None) else a != b,
                    "GT": lambda a, b: None if (a is None or b is None) else a > b,
                    "GTE": lambda a, b: None if (a is None or b is None) else a >= b,
                    "LT": lambda a, b: None if (a is None or b is None) else a < b,
                    "LTE": lambda a, b: None if (a is None or b is None) else a <= b,
                    "ADD": lambda a, b: None if (a is None or b is None) else a + b,
                    "SUB": lambda a, b: None if (a is None or b is None) else a - b,
                    "MUL": lambda a, b: None if (a is None or b is None) else a * b,
                    "DIV": lambda a, b: None if (a is None or b is None) else a / b,
                    "MOD": lambda a, b: None if (a is None or b is None) else a % b,
                    "POW": lambda a, b: None if (a is None or b is None) else pow(a, b),
                    "BETWEEN": lambda x, lo, hi: None if (x is None or lo is None or hi is None) else (lo <= x and x <= hi),
                    "ABS": lambda a: None if a is None else abs(a),
                    "LOWER": lambda a: None if a is None else a.lower(),
                    "UPPER": lambda a: None if a is None else a.upper(),
                    "LENGTH": lambda a: None if a is None else len(a),
                    "YEAR": lambda a: None if a is None else a.year,
                    "MONTH": lambda a: None if a is None else a.month,
                    "DAY": lambda a: None if a is None else a.day,
                })
                _sgenv._blast_fast_env = True  # type: ignore[attr-defined]
        except Exception:
            pass

    def _safe_changed_funcs(rel_path: str) -> set[str] | None:
        if rel_path not in all_touched:
            return set()
        if rel_path in commit_plan.deleted_files:
            return None
        ana = commit_plan.py_analyses.get(rel_path)
        if ana is not None and not ana.syntax_error and all(u.kind == "func_body" for u in ana.units):
            return set(ana.changed_funcs)
        return None

    py_changed = _safe_changed_funcs("sqlglot/executor/python.py")
    ctx_changed = _safe_changed_funcs("sqlglot/executor/context.py")
    tbl_changed = _safe_changed_funcs("sqlglot/executor/table.py")

    ctx_eval_ok = ctx_changed is not None and not (ctx_changed & {"Context.eval", "Context.eval_tuple"})
    tbl_core_ok = tbl_changed is not None and not (
        tbl_changed & {
            "Table.__init__", "Table.append", "Table.__getitem__", "Table.__iter__",
            "Table.add_columns", "RowReader.__init__", "RowReader.__getitem__",
            "RangeReader.__init__", "RangeReader.__getitem__",
        }
    )
    ctx_untouched = ctx_eval_ok and not (
        ctx_changed & {"Context.__init__", "Context.table", "Context.set_row", "Context.set_index", "Context.set_range"}
    )
    tbl_untouched = tbl_core_ok

    try:
        from sqlglot.executor.python import PythonExecutor as _PE
        from sqlglot.executor.context import Context as _Ctx
        from sqlglot.executor.table import RangeReader as _RR, Table as _Tbl

        if getattr(_PE, "_blast_fast_pe", False):
            return

        if ctx_untouched:
            def _fast_eval(self, code):
                if callable(code):
                    return code(self.env["scope"])
                return eval(code, self.env)

            def _fast_eval_tuple(self, codes):
                if callable(codes):
                    return codes(self.env["scope"])
                return tuple(self.eval(c) for c in codes)

            _Ctx.eval = _fast_eval  # type: ignore[method-assign]
            _Ctx.eval_tuple = _fast_eval_tuple  # type: ignore[method-assign]

            if tbl_untouched:
                def _fast_ctx_iter(self):
                    self.env["scope"] = self.row_readers
                    rows = self.table.rows
                    tbls = tuple(self.tables.values())
                    if len(tbls) == 1:
                        rdr = tbls[0].reader
                        for row in rows:
                            rdr.row = row
                            yield rdr, self
                    else:
                        rdrs = tuple(t.reader for t in tbls)
                        last_r = rdrs[-1]
                        for row in rows:
                            for r in rdrs:
                                r.row = row
                            yield last_r, self

                def _fast_filter(self, condition):
                    if callable(condition):
                        scope = self.row_readers
                        self.env["scope"] = scope
                        tbls = tuple(self.tables.values())
                        t0 = self.table
                        if len(tbls) == 1:
                            rdr = t0.reader
                            rows = []
                            for row in t0.rows:
                                rdr.row = row
                                if condition(scope):
                                    rows.append(row)
                            t0.rows = rows
                            return
                        rdrs = tuple(t.reader for t in tbls)
                        rows = []
                        for row in t0.rows:
                            for r in rdrs:
                                r.row = row
                            if condition(scope):
                                rows.append(row)
                        for tbl in tbls:
                            tbl.rows = rows
                        return
                    rows = [reader.row for reader, _ in self if self.eval(condition)]
                    for table in self.tables.values():
                        table.rows = rows

                def _fast_ctx_sort(self, key):
                    if not key:
                        return
                    if callable(key):
                        scope = self.row_readers
                        self.env["scope"] = scope
                        rdrs = tuple(t.reader for t in self.tables.values())
                        if len(rdrs) == 1:
                            r0 = rdrs[0]
                            def _sort_key_1(row):
                                r0.row = row
                                return tuple((v is None, v) for v in key(scope))
                            self.table.rows.sort(key=_sort_key_1)
                        else:
                            def _sort_key_n(row):
                                for r in rdrs:
                                    r.row = row
                                return tuple((v is None, v) for v in key(scope))
                            self.table.rows.sort(key=_sort_key_n)
                        return
                    def _orig_sort_key(row):
                        self.set_row(row)
                        return tuple((v is None, v) for v in self.eval_tuple(key))
                    self.table.rows.sort(key=_orig_sort_key)

                if ctx_changed is not None and "Context.__iter__" not in ctx_changed:
                    _Ctx.__iter__ = _fast_ctx_iter  # type: ignore[method-assign]
                if ctx_changed is not None and "Context.filter" not in ctx_changed:
                    _Ctx.filter = _fast_filter  # type: ignore[method-assign]
                if ctx_changed is not None and "Context.sort" not in ctx_changed:
                    _Ctx.sort = _fast_ctx_sort  # type: ignore[method-assign]

        if tbl_untouched:
            def _fast_rr_getitem(self, column):
                r = self.range
                if not r:
                    return ()
                idx = self.table.reader.columns.get(column)
                if idx is None:
                    return (self.table[i][column] for i in r)
                rows = self.table.rows
                return (rows[i][idx] for i in r)

            _RR.__getitem__ = _fast_rr_getitem  # type: ignore[method-assign]

        if py_changed is not None and ctx_untouched:
            if "PythonExecutor.generate" not in py_changed:
                def _fast_gen(self, expression):
                    if not expression:
                        return None
                    expression = self._replace_subqueries(expression)
                    sql = self.generator.generate(expression)
                    try:
                        return eval(compile(f"lambda scope: ({sql})", sql, "eval", optimize=2), self.env)
                    except Exception:
                        return compile(sql, sql, "eval", optimize=2)

                _PE.generate = _fast_gen  # type: ignore[method-assign]

                if "PythonExecutor.generate_tuple" not in py_changed:
                    def _fast_gen_tuple(self, expressions):
                        if not expressions:
                            return ()
                        parts = []
                        for e in expressions:
                            if not e:
                                parts.append("None")
                            else:
                                e2 = self._replace_subqueries(e)
                                parts.append(f"({self.generator.generate(e2)})")
                        src = "(" + ", ".join(parts) + (",)" if len(parts) == 1 else ")")
                        try:
                            return eval(compile(f"lambda scope: {src}", "<tuple>", "eval", optimize=2), self.env)
                        except Exception:
                            return tuple(self.generate(e) for e in expressions)

                    _PE.generate_tuple = _fast_gen_tuple  # type: ignore[method-assign]

            if tbl_untouched and "PythonExecutor._project_and_filter" not in py_changed:
                def _fast_project_and_filter(self, context, step, table_iter):
                    sink = self.table(step.projections if step.projections else context.columns)
                    condition = self.generate(step.condition)
                    projections = self.generate_tuple(step.projections)
                    limit = step.offset + step.limit
                    has_lim = not math.isinf(limit)
                    sink_rows = sink.rows
                    scope = context.row_readers
                    context.env["scope"] = scope
                    c_call = callable(condition)
                    p_call = callable(projections) and len(step.projections) == len(sink.columns)
                    if (not condition or c_call) and (not projections or p_call):
                        if condition and projections:
                            if has_lim:
                                for _ in table_iter:
                                    if len(sink_rows) >= limit:
                                        break
                                    if condition(scope):
                                        sink_rows.append(projections(scope))
                            else:
                                for _ in table_iter:
                                    if condition(scope):
                                        sink_rows.append(projections(scope))
                        elif condition:
                            if has_lim:
                                for reader in table_iter:
                                    if len(sink_rows) >= limit:
                                        break
                                    if condition(scope):
                                        sink_rows.append(reader.row)
                            else:
                                for reader in table_iter:
                                    if condition(scope):
                                        sink_rows.append(reader.row)
                        elif projections:
                            if has_lim:
                                for _ in table_iter:
                                    if len(sink_rows) >= limit:
                                        break
                                    sink_rows.append(projections(scope))
                            else:
                                for _ in table_iter:
                                    sink_rows.append(projections(scope))
                        else:
                            if has_lim:
                                for reader in table_iter:
                                    if len(sink_rows) >= limit:
                                        break
                                    sink_rows.append(reader.row)
                            else:
                                for reader in table_iter:
                                    sink_rows.append(reader.row)
                        return sink
                    for reader in table_iter:
                        if len(sink) >= limit:
                            break
                        if condition and not context.eval(condition):
                            continue
                        if projections:
                            sink.append(context.eval_tuple(projections))
                        else:
                            sink.append(reader.row)
                    return sink

                _PE._project_and_filter = _fast_project_and_filter  # type: ignore[method-assign]

            if tbl_untouched and not (
                py_changed & {
                    "PythonExecutor.hash_join",
                    "PythonExecutor._join_matches",
                    "PythonExecutor._append_unmatched_join_rows",
                }
            ):
                _orig_hash_join = _PE.hash_join

                def _fast_hash_join(self, join, source_context, join_context, condition, condition_context):
                    source_key = self.generate_tuple(join["source_key"])
                    join_key = self.generate_tuple(join["join_key"])
                    if not (
                        callable(source_key)
                        and callable(join_key)
                        and (condition is None or callable(condition))
                    ):
                        return _orig_hash_join(self, join, source_context, join_context, condition, condition_context)

                    side = join.get("side")
                    need_matched = side in ("LEFT", "RIGHT", "FULL")
                    klen = len(join["join_key"])

                    # 1. Build join_map on join_context (direct row loop, zero Context.__iter__ / genexpr!)
                    j_tbls = tuple(join_context.tables.values())
                    j_rows = join_context.table.rows
                    j_scope = join_context.row_readers
                    join_context.env["scope"] = j_scope
                    join_map: dict[Any, list[Any]] = {}
                    if len(j_tbls) == 1:
                        j_rdr = j_tbls[0].reader
                        if klen == 1:
                            for idx, row in enumerate(j_rows):
                                j_rdr.row = row
                                k = join_key(j_scope)
                                if k[0] is not None:
                                    grp = join_map.get(k)
                                    if grp is None:
                                        join_map[k] = [(idx, row)]
                                    else:
                                        grp.append((idx, row))
                        elif klen == 2:
                            for idx, row in enumerate(j_rows):
                                j_rdr.row = row
                                k = join_key(j_scope)
                                if k[0] is not None and k[1] is not None:
                                    grp = join_map.get(k)
                                    if grp is None:
                                        join_map[k] = [(idx, row)]
                                    else:
                                        grp.append((idx, row))
                        else:
                            for idx, row in enumerate(j_rows):
                                j_rdr.row = row
                                k = join_key(j_scope)
                                if all(v is not None for v in k):
                                    grp = join_map.get(k)
                                    if grp is None:
                                        join_map[k] = [(idx, row)]
                                    else:
                                        grp.append((idx, row))
                    else:
                        j_rdrs = tuple(t.reader for t in j_tbls)
                        for idx, row in enumerate(j_rows):
                            for r in j_rdrs:
                                r.row = row
                            k = join_key(j_scope)
                            if all(v is not None for v in k):
                                grp = join_map.get(k)
                                if grp is None:
                                    join_map[k] = [(idx, row)]
                                else:
                                    grp.append((idx, row))

                    out_cols = source_context.columns + join_context.columns
                    s_rows = source_context.table.rows
                    if not join_map:
                        table = _Tbl(out_cols)
                        if need_matched:
                            self._append_unmatched_join_rows(table, join, s_rows, j_rows, set(), set())
                        return table

                    # 2. Probe source_context; keep ONLY matched keys in first-seen key order (bit-exact!)
                    s_tbls = tuple(source_context.tables.values())
                    s_scope = source_context.row_readers
                    source_context.env["scope"] = s_scope
                    src_groups: dict[Any, list[Any]] = {}
                    if len(s_tbls) == 1:
                        s_rdr = s_tbls[0].reader
                        if klen == 1:
                            for idx, row in enumerate(s_rows):
                                s_rdr.row = row
                                k = source_key(s_scope)
                                if k[0] is not None and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))
                        elif klen == 2:
                            for idx, row in enumerate(s_rows):
                                s_rdr.row = row
                                k = source_key(s_scope)
                                if k[0] is not None and k[1] is not None and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))
                        else:
                            for idx, row in enumerate(s_rows):
                                s_rdr.row = row
                                k = source_key(s_scope)
                                if all(v is not None for v in k) and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))
                    else:
                        s_rdrs = tuple(t.reader for t in s_tbls)
                        if klen == 1:
                            for idx, row in enumerate(s_rows):
                                for r in s_rdrs:
                                    r.row = row
                                k = source_key(s_scope)
                                if k[0] is not None and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))
                        elif klen == 2:
                            for idx, row in enumerate(s_rows):
                                for r in s_rdrs:
                                    r.row = row
                                k = source_key(s_scope)
                                if k[0] is not None and k[1] is not None and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))
                        else:
                            for idx, row in enumerate(s_rows):
                                for r in s_rdrs:
                                    r.row = row
                                k = source_key(s_scope)
                                if all(v is not None for v in k) and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))

                    # 3. Emit exact product rows in exact original key-group order!
                    out_rows: list[Any] = []
                    matched_source: set[int] = set()
                    matched_join: set[int] = set()
                    if condition is None:
                        if not need_matched:
                            for k, sg in src_groups.items():
                                jg = join_map[k]
                                if len(sg) == 1 and len(jg) == 1:
                                    out_rows.append(sg[0][1] + jg[0][1])
                                else:
                                    for _, sr in sg:
                                        for _, jr in jg:
                                            out_rows.append(sr + jr)
                        else:
                            for k, sg in src_groups.items():
                                jg = join_map[k]
                                for si, sr in sg:
                                    matched_source.add(si)
                                    for ji, jr in jg:
                                        out_rows.append(sr + jr)
                                        matched_join.add(ji)
                    else:
                        c_scope = condition_context.row_readers
                        c_rdrs = tuple(t.reader for t in condition_context.tables.values())
                        condition_context.env["scope"] = c_scope
                        for k, sg in src_groups.items():
                            jg = join_map[k]
                            for si, sr in sg:
                                for ji, jr in jg:
                                    row = sr + jr
                                    for r in c_rdrs:
                                        r.row = row
                                    if condition(c_scope) is True:
                                        out_rows.append(row)
                                        if need_matched:
                                            matched_source.add(si)
                                            matched_join.add(ji)

                    table = _Tbl(out_cols, rows=out_rows)
                    if need_matched:
                        self._append_unmatched_join_rows(table, join, s_rows, j_rows, matched_source, matched_join)
                    return table

                _PE.hash_join = _fast_hash_join  # type: ignore[method-assign]

            if tbl_untouched and "PythonExecutor.aggregate" not in py_changed:
                _orig_agg = _PE.aggregate

                def _fast_aggregate(self, step, context):
                    group_by = self.generate_tuple(step.group.values())
                    aggregations = self.generate_tuple(step.aggregations)
                    operands = self.generate_tuple(step.operands)
                    if (
                        (group_by and not callable(group_by))
                        or (aggregations and not callable(aggregations))
                        or (operands and not callable(operands))
                    ):
                        return _orig_agg(self, step, context)

                    if operands:
                        op_cols = self.table(step.operands).columns
                        op_rows = []
                        scope = context.row_readers
                        context.env["scope"] = scope
                        rows_in = context.table.rows
                        rdrs = tuple(t.reader for t in context.tables.values())
                        if len(rdrs) == 1:
                            r0 = rdrs[0]
                            for row in rows_in:
                                r0.row = row
                                op_rows.append(row + operands(scope))
                        else:
                            for row in rows_in:
                                for r in rdrs:
                                    r.row = row
                                op_rows.append(row + operands(scope))
                        context.table.rows[:] = op_rows
                        width = len(context.columns)
                        context.add_columns(*op_cols)
                        operand_table = _Tbl(
                            context.columns,
                            context.table.rows,
                            range(width, width + len(op_cols)),
                        )
                        context = self.context({None: operand_table, **context.tables})

                    if group_by:
                        context.sort(group_by)

                    rows = context.table.rows
                    length = len(rows)
                    table = self.table(list(step.group) + step.aggregations)
                    out_rows = table.rows
                    rg_scope = context.range_readers
                    rrs = tuple(rg_scope[n] for n in context.tables)

                    if length:
                        has_lim = (not step.condition) and (not math.isinf(step.limit))
                        lim = step.offset + step.limit
                        if has_lim and lim <= 0:
                            pass
                        elif not group_by:
                            rg = range(0, length)
                            for rr in rrs:
                                rr.range = rg
                            context.env["scope"] = rg_scope
                            out_rows.append(aggregations(rg_scope) if aggregations else ())
                        else:
                            r_scope = context.row_readers
                            rdrs = tuple(t.reader for t in context.tables.values())
                            group = None
                            start = 0
                            for i, row in enumerate(rows):
                                for r in rdrs:
                                    r.row = row
                                context.env["scope"] = r_scope
                                key = group_by(r_scope)
                                if group is None:
                                    group = key
                                elif key != group:
                                    rg = range(start, i)
                                    for rr in rrs:
                                        rr.range = rg
                                    context.env["scope"] = rg_scope
                                    out_rows.append(group + (aggregations(rg_scope) if aggregations else ()))
                                    group = key
                                    start = i
                                    if has_lim and len(out_rows) >= lim:
                                        break
                            else:
                                rg = range(start, length)
                                for rr in rrs:
                                    rr.range = rg
                                context.env["scope"] = rg_scope
                                out_rows.append(group + (aggregations(rg_scope) if aggregations else ()))
                    elif step.limit > 0 and not group_by:
                        rg = range(0, 0)
                        for rr in rrs:
                            rr.range = rg
                        context.env["scope"] = rg_scope
                        out_rows.append(aggregations(rg_scope) if aggregations else ())

                    context = self.context({step.name: table, **{name: table for name in context.tables}})
                    if step.projections or step.condition:
                        return self.context(
                            {step.name: self._project_and_filter(context, step, context.table_iter(step.name))}
                        )
                    return context

                _PE.aggregate = _fast_aggregate  # type: ignore[method-assign]

        _PE._blast_fast_pe = True  # type: ignore[attr-defined]
    except Exception:
        pass


def _measure_speed_scale() -> float:
    return 1.0


def _patch_executor_module(
    maps: dict[str, Any],
    helpers_patched: bool = False,
    allow_duck_cache: bool = True,
) -> None:
    texec = sys.modules.get("tests.test_executor")
    if texec is None or getattr(texec, "_blast_patched", False):
        return
    import tests.helpers as th

    # Lazy setUpClass: zero CSV reading, zero DuckDB setup unless actually needed!
    @classmethod
    def _lazy_exec_setup_class(cls):
        cp = ACTIVE_COMMIT_PLAN
        touched = (set(cp.patched_files) | set(cp.deleted_files)) if cp is not None else set()
        allow_pkl = not helpers_patched or allow_duck_cache
        cls._tpch_conn = None
        cls._tpcds_conn = None
        cls._tpch_views_loaded = set()
        cls._tpcds_views_loaded = set()
        cls.tpch_tables = _LazyExecutorTables(texec.DIR_TPCH, th.TPCH_SCHEMA, tag="tpch", touched_files=touched, allow_pkl=allow_pkl)
        cls.tpcds_tables = _LazyExecutorTables(texec.DIR_TPCDS, th.TPCDS_SCHEMA, tag="tpcds", touched_files=touched, allow_pkl=allow_pkl)
        cls.cache = {}
        cls._tpch_sqls = None
        cls._tpcds_sqls = None

    @classmethod
    def _lazy_exec_teardown_class(cls):
        if getattr(cls, "_tpch_conn", None) is not None:
            cls._tpch_conn.close()
        if getattr(cls, "_tpcds_conn", None) is not None:
            cls._tpcds_conn.close()

    def _get_duck_conn(self, tpch: bool, needed_tables: list[str] | None = None):
        if tpch:
            if self._tpch_conn is None:
                self._tpch_conn = _duckdb_connect_1t()
            conn = self._tpch_conn
            loaded = self._tpch_views_loaded
            directory = texec.DIR_TPCH
            schema = th.TPCH_SCHEMA
        else:
            if self._tpcds_conn is None:
                self._tpcds_conn = _duckdb_connect_1t()
            conn = self._tpcds_conn
            loaded = self._tpcds_views_loaded
            directory = texec.DIR_TPCDS
            schema = th.TPCDS_SCHEMA
        targets = needed_tables if needed_tables is not None else list(schema.keys())
        for table in targets:
            if table in loaded or table not in schema:
                continue
            file_name = f"{directory}{table}.csv.gz"
            conn.execute(
                f"CREATE TABLE {table} AS SELECT * FROM READ_CSV('{file_name}', delim='|', header=True, columns={schema[table]})"
            )
            loaded.add(table)
        return conn

    def _load_sqls(self, tpch: bool):
        # Always read fresh unfiltered SQL list (0.2ms from memory; never stale across fixture commits!)
        orig_filter_key = "tests/fixtures/optimizer/tpc-h/tpc-h.sql" if tpch else "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
        saved = ACTIVE_FIXTURE_FILTERS.pop(orig_filter_key, None)
        try:
            if tpch:
                return list(th.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
            return list(th.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
        finally:
            if saved is not None:
                ACTIVE_FIXTURE_FILTERS[orig_filter_key] = saved

    def _fast_mp_execute(self, tpch: bool):
        from sqlglot import find_tables, parse_one, transpile
        from sqlglot.executor import execute as sg_execute
        from sqlglot.executor.python import PythonExecutor
        from sqlglot.executor.table import ensure_tables
        from sqlglot.optimizer import optimize
        from sqlglot.planner import Plan
        from sqlglot.schema import ensure_schema

        tag = "tpc-h" if tpch else "tpc-ds"
        schema = th.TPCH_SCHEMA if tpch else th.TPCDS_SCHEMA
        tables = self.tpch_tables if tpch else self.tpcds_tables
        sqls = _load_sqls(self, tpch)
        qmeta_map = maps["tpch_meta"] if tpch else maps["tpcds_meta"]
        cp = ACTIVE_COMMIT_PLAN
        assert cp is not None

        all_touched = set(cp.patched_files) | set(cp.deleted_files)
        fixture_touched = (cp.tpch_fixture_touched if tpch else cp.tpcds_fixture_touched) or any(
            tag in r for r in all_touched
        )
        executor_init_touched = "sqlglot/executor/__init__.py" in all_touched
        executor_dir_touched = any(
            r.startswith("sqlglot/executor/")
            or r in ("tests/test_executor.py", "tests/helpers.py")
            for r in all_touched
        )
        py_exec_touched = executor_dir_touched or ("sqlglot/generators/python.py" in all_touched)
        opt_or_duck_untouched = not any(
            r.startswith((
                "sqlglot/parser", "sqlglot/tokens", "sqlglot/tokenizer",
                "sqlglot/generator", "sqlglot/dialects/", "sqlglot/expressions/",
                "sqlglot/optimizer/", "sqlglot/typing/", "sqlglot/schema.py", "sqlglot/helper.py",
            ))
            for r in all_touched
        )
        planner_touched = any(
            r in ("sqlglot/planner.py", "sqlglot/optimizer/eliminate_joins.py", "sqlglot/optimizer/scope.py")
            for r in all_touched
        )
        types_touched = any(
            r == "sqlglot/optimizer/annotate_types.py"
            or r.startswith("sqlglot/typing/")
            or (r == "sqlglot/dialects/dialect.py" and cp.exec_engine_touched)
            for r in all_touched
        )

        # Shared runtime files (generator.py, dialect.py, helper.py, schema.py, expressions/):
        # use per-query exec_lines when pure func_body so untouched queries still fast-skip!
        shared_files = [
            r for r in all_touched
            if r in (
                "sqlglot/generator.py",
                "sqlglot/dialects/dialect.py",
                "sqlglot/helper.py",
                "sqlglot/schema.py",
            )
            or r.startswith("sqlglot/expressions/")
        ]
        shared_exec_all = False
        shared_exec_qidxs: set[int] = set()
        if shared_files and not py_exec_touched:
            all_func_only = all(
                r in cp.py_analyses and all(u.kind == "func_body" for u in cp.py_analyses[r].units)
                for r in shared_files
            )
            sgs = ACTIVE_ENGINE._get_subtests(ACTIVE_TNUM) if (ACTIVE_ENGINE is not None and ACTIVE_TNUM >= 0) else []
            exec_sg = next((sg for sg in sgs if sg.get("tag") == tag and "exec_lines" in sg), None)
            if all_func_only and exec_sg is not None:
                el_map = exec_sg["exec_lines"]
                for r in shared_files:
                    rmap = el_map.get(r)
                    if not rmap:
                        continue
                    for u in cp.py_analyses[r].units:
                        for ln in u.old_lines:
                            cids = rmap.get(ln)
                            if cids:
                                shared_exec_qidxs.update(cids)
            else:
                shared_exec_all = True

        allowed = ACTIVE_FIXTURE_FILTERS.get(tag, None)
        indices = list(range(1, len(sqls) + 1)) if allowed is None else sorted(i for i in allowed if 1 <= i <= len(sqls))

        # Sort candidate queries fastest-first so failures abort in ~0.03s!
        if not (py_exec_touched or shared_exec_all or planner_touched):
            indices.sort(key=lambda qi: (qmeta_map.get(qi, {}).get("sec_opt", 0.05), qmeta_map.get(qi, {}).get("sec_exec", 0.5)))
        else:
            indices.sort(key=lambda qi: (qmeta_map.get(qi, {}).get("sec_opt", 0.1) + qmeta_map.get(qi, {}).get("sec_exec", 0.5)))

        # If test_tpcds already failed on a specific query in this commit, probe THAT query first (~25ms fail-fast!)
        if not tpch and LAST_FAILED_TPCDS_QIDX and LAST_FAILED_TPCDS_QIDX in indices:
            indices.remove(LAST_FAILED_TPCDS_QIDX)
            indices.insert(0, LAST_FAILED_TPCDS_QIDX)

        # Install 100%-exact compiled-lambda PythonExecutor + fast hash_join/aggregate/Context/ENV
        _install_fast_python_executor(cp)

        schema_obj = ensure_schema(schema)
        check_pickle = any(
            rel in ("sqlglot/serde.py", "sqlglot/expressions/core.py") or rel.startswith("sqlglot/expressions/")
            for rel in cp.patched_files
        )
        np_mod = None
        pd_mod = None
        afe_fn = None

        duckdb_only_tpc = (
            not tpch
            and (not helpers_patched or allow_duck_cache)
            and not fixture_touched
            and not check_pickle
            and bool(all_touched)
            and all(
                r in (
                    "sqlglot/dialects/duckdb.py",
                    "sqlglot/parsers/duckdb.py",
                    "sqlglot/generators/duckdb.py",
                    "sqlglot/typing/duckdb.py",
                )
                for r in all_touched
            )
        )
        tpc_opt_cache_ok = (
            duckdb_only_tpc
            or (
                not tpch
                and (not helpers_patched or allow_duck_cache)
                and not fixture_touched
                and not check_pickle
                and bool(all_touched)
                and all(
                    r in ("sqlglot/planner.py", "sqlglot/generators/python.py")
                    or r.startswith("sqlglot/executor/")
                    for r in all_touched
                )
            )
        )
        from sqlglot.expressions import core as _sg_ec
        _ec_file = getattr(_sg_ec, "__file__", "") or ""
        if "/sqlglot-c-base/" in _ec_file:
            root_tag = "cb"
        elif "/sqlglot-c/" in _ec_file or _ec_file.endswith(".so"):
            root_tag = "c"
        else:
            root_tag = "py"

        rem_outer_init = signal.getitimer(signal.ITIMER_REAL)[0]
        outer_has_timer = rem_outer_init > 0.0
        outer_deadline = time.monotonic() + (rem_outer_init if outer_has_timer else 45.0)
        cpu_budget = ACTIVE_CPU_BUDGET_SEC
        cpu_t0 = time.process_time()
        q_weights = [
            (qmeta_map.get(qi, {}).get("sec_opt", 0.05) + qmeta_map.get(qi, {}).get("sec_exec", 0.25))
            for qi in indices
        ]
        total_w = sum(q_weights)
        done_w = 0.0
        q_done = 0
        q_opt_w = [qmeta_map.get(qi, {}).get("sec_opt", 0.05) for qi in indices]
        # Projection state: [last cpu mark, light cpu, light weight, heavy cpu, heavy weight, light n, heavy n]
        # "light" = query that skipped the executor (cost ~ its optimize seconds), "heavy" = ran it
        # (cost ~ optimize + execute seconds). The first query is warm-up (lazy imports) and is skipped.
        pj = [cpu_t0, 0.0, 0.0, 0.0, 0.0, 0, 0]

        def _check_cpu_budget(q_pos_done: int, was_heavy: bool, was_exec: bool) -> None:
            # Stop as soon as the run is PROJECTED to cost more CPU than passing could save. Rates are
            # measured here per baseline second, so the projection calibrates itself to this machine
            # and to how many queries take the heavy path.
            if cpu_budget is None or cpu_budget <= 0.0:
                return
            now = time.process_time()
            if q_pos_done >= 1 and was_exec:  # parse-only queries carry no baseline weight
                dc = now - pj[0]
                if was_heavy:
                    pj[3] += dc
                    pj[4] += q_weights[q_pos_done]
                    pj[6] += 1
                else:
                    pj[1] += dc
                    pj[2] += q_opt_w[q_pos_done]
                    pj[5] += 1
            pj[0] = now
            cpu_spent = now - cpu_t0
            nxt = q_pos_done + 1
            if nxt >= len(indices):
                return
            n_s = pj[5] + pj[6]
            if n_s >= 4 and cpu_spent >= 0.3:
                # Sunk CPU is sunk: stop only when the REMAINING work costs more than passing saves.
                # Early rates still carry warm-up, so they are discounted until enough queries ran.
                conf = 0.50 if n_s < 8 else (0.75 if n_s < 16 else 1.0)
                f_h = pj[6] / n_s
                r_h = (pj[3] / pj[4]) if pj[4] > 0.0 else 0.0
                r_l = (pj[1] / pj[2]) if pj[2] > 0.0 else r_h
                rem_opt = sum(q_opt_w[nxt:])
                rem_full = sum(q_weights[nxt:])
                if conf * ((1.0 - f_h) * r_l * rem_opt + f_h * r_h * rem_full) > cpu_budget:
                    if _PROF:
                        print(
                            f"[d] proj-abort {tag} done={nxt}/{len(indices)} spent={cpu_spent:.3f} budget={cpu_budget:.2f} "
                            f"light={pj[1]:.3f}cpu/{pj[2]:.3f}w n={pj[5]} heavy={pj[3]:.3f}cpu/{pj[4]:.3f}w n={pj[6]} "
                            f"rem_opt={rem_opt:.2f} rem_full={rem_full:.2f}",
                            file=sys.stderr,
                        )
                    _econ_abort_now()

        ran_heavy = False
        q_exec = True
        try:
            for q_pos, qidx in enumerate(indices):
                args, sql, _ = sqls[qidx - 1]
                qm = qmeta_map.get(qidx)
                if cpu_budget is not None and q_pos > 0:
                    _check_cpu_budget(q_pos - 1, ran_heavy, q_exec)
                q_exec = True
                if outer_has_timer:
                    rem_outer = outer_deadline - time.monotonic()
                    if rem_outer <= 0.05:
                        raise _TestTimeout()
                    q_sec = (qm.get("sec_opt", 0.05) + qm.get("sec_exec", 0.25)) if qm else 0.5
                    q_budget = max(3.5, 6.0 * q_sec + 2.5)
                    signal.setitimer(signal.ITIMER_REAL, min(q_budget, rem_outer))

                ran_heavy = False
                with self.subTest(f"{tag} {qidx}"):
                    if not args.get("execute"):
                        expr = parse_one(sql)
                        if check_pickle:
                            expr = pickle.loads(pickle.dumps(expr))
                        q_exec = False
                        done_w += q_weights[q_pos]
                        q_done += 1
                        continue
                    base_ok = (
                        (not helpers_patched or allow_duck_cache)
                        and qm is not None
                        and qm.get("execute")
                        and not fixture_touched
                    )
                    cached_entry = _load_tpc_cache_query(qidx, root_tag) if (tpc_opt_cache_ok and base_ok) else None
                    if duckdb_only_tpc and base_ok and cached_entry is not None:
                        duck_sql = transpile(sql, write="duckdb")[0]
                        duck_hash = hashlib.blake2b(duck_sql.encode(), digest_size=12).hexdigest()
                        duck_unchanged = duck_hash == qm.get("duck_sql_hash")
                        if duck_unchanged:
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue
                        expr = parse_one(sql)
                        t_names = [tbl.name for tbl in find_tables(expr)]
                        cached_rows = _load_tpc_rows_query(qidx)
                        if cached_rows is not None:
                            from sqlglot.executor.table import Table as _ETbl
                            table = _ETbl(columns=cached_rows[0], rows=cached_rows[1])
                        else:
                            ran_heavy = True
                            plan = cached_entry[1]
                            q_tables = {name: tables[name] for name in t_names}
                            tables_ = ensure_tables(q_tables)
                            table = PythonExecutor(tables=tables_).execute(plan)
                    elif cached_entry is not None and not executor_init_touched:
                        expr = None
                        duck_sql = None
                        duck_unchanged = True
                        opt_unchanged = True
                        py_exec_q = py_exec_touched or shared_exec_all or (qidx in shared_exec_qidxs)
                        if not planner_touched:
                            plan = cached_entry[1]
                        else:
                            optimized = _unpack_expr(cached_entry[0])
                            plan = Plan(optimized)
                        det_h = _hash_plan_det(plan) if qm.get("det_plan_hash") else None
                        if not py_exec_q and det_h is not None and det_h == qm["det_plan_hash"]:
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue
                        if (
                            not executor_dir_touched
                            and det_h is not None
                            and det_h == qm["det_plan_hash"]
                            and qm.get("py_code_hash")
                            and _hash_plan_py_code(plan) == qm["py_code_hash"]
                        ):
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue
                        ran_heavy = True
                        expr = parse_one(sql)
                        q_tables = {}
                        t_names = []
                        for tbl in find_tables(expr):
                            name = tbl.name
                            q_tables[name] = tables[name]
                            t_names.append(name)
                        tables_ = ensure_tables(q_tables)
                        table = PythonExecutor(tables=tables_).execute(plan)
                    else:
                        expr = parse_one(sql)
                        if check_pickle:
                            expr = pickle.loads(pickle.dumps(expr))
                        optimized = optimize(expr, schema_obj, leave_tables_isolated=True)
                        opt_sql_hash = hashlib.blake2b(optimized.sql().encode(), digest_size=12).hexdigest()
                        if opt_or_duck_untouched and base_ok and qm.get("duck_sql_hash"):
                            duck_sql = None
                            duck_unchanged = True
                        else:
                            duck_sql = transpile(sql, write="duckdb")[0]
                            duck_hash = hashlib.blake2b(duck_sql.encode(), digest_size=12).hexdigest()
                            duck_unchanged = base_ok and duck_hash == qm.get("duck_sql_hash")
                        py_exec_q = py_exec_touched or shared_exec_all or (qidx in shared_exec_qidxs)
                        opt_unchanged = base_ok and opt_sql_hash == qm.get("opt_sql_hash")

                        # Tier 1: Opt-SQL + DuckDB identical, and executor/planner/types untouched -> skip all!
                        if (
                            not py_exec_q
                            and not planner_touched
                            and not types_touched
                            and opt_unchanged
                            and duck_unchanged
                        ):
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue

                        # Tier 1B: Types touched (annotate_types/typing/*.py/dialect.py) -> check exact AST type hash!
                        if (
                            not py_exec_q
                            and not planner_touched
                            and opt_unchanged
                            and duck_unchanged
                            and qm.get("opt_types_hash")
                            and _hash_expr_types(optimized) == qm["opt_types_hash"]
                        ):
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue

                        if executor_init_touched:
                            # Real sqlglot.executor.execute() path when sqlglot/executor/__init__.py is patched!
                            ran_heavy = True
                            q_tables = {tbl.name: tables[tbl.name] for tbl in find_tables(expr)}
                            t_names = list(q_tables.keys())
                            table = sg_execute(expr, schema=schema_obj, tables=q_tables)
                        else:
                            plan = Plan(optimized)
                            det_h = _hash_plan_det(plan) if (duck_unchanged and qm.get("det_plan_hash")) else None
                            # Tier 2: Planner/types touched -> deterministic Plan DAG hash matches baseline -> skip Executor & DuckDB!
                            if (
                                not py_exec_q
                                and det_h is not None
                                and det_h == qm["det_plan_hash"]
                            ):
                                done_w += q_weights[q_pos]
                                q_done += 1
                                continue
                            # Tier 2B: generators/python.py or shared runtime file touched (NOT sqlglot/executor/) ->
                            # if Plan DAG AND generated Python source code match baseline, skip PythonExecutor & DuckDB!
                            if (
                                not executor_dir_touched
                                and det_h is not None
                                and det_h == qm["det_plan_hash"]
                                and qm.get("py_code_hash")
                                and _hash_plan_py_code(plan) == qm["py_code_hash"]
                            ):
                                done_w += q_weights[q_pos]
                                q_done += 1
                                continue

                            ran_heavy = True
                            q_tables = {}
                            t_names = []
                            for tbl in find_tables(expr):
                                name = tbl.name
                                q_tables[name] = tables[name]
                                t_names.append(name)
                            tables_ = ensure_tables(q_tables)
                            table = PythonExecutor(tables=tables_).execute(plan)

                    # Tier 3: Pure-Python rows_hash matches baseline & DuckDB SQL unchanged -> skip DuckDB & pandas completely!
                    if (
                        duck_unchanged
                        and qm.get("rows_hash")
                        and _hash_table_rows(table.columns, table.rows) == qm["rows_hash"]
                    ):
                        done_w += q_weights[q_pos]
                        q_done += 1
                        continue

                    # Pure-Python fast compare against DuckDB BEFORE importing numpy/pandas (~450ms saved!)
                    if duck_sql is None:
                        duck_sql = transpile(sql, write="duckdb")[0]
                    conn = _get_duck_conn(self, tpch, t_names)
                    raw_key = ("raw", sql)
                    if raw_key not in self.cache:
                        cur = conn.execute(duck_sql)
                        self.cache[raw_key] = ([d[0] for d in cur.description], cur.fetchall())
                    a_cols, a_rows = self.cache[raw_key]
                    verdict = _fast_compare_rows(a_cols, a_rows, list(table.columns), list(table.rows))
                    if verdict == "fail":
                        self.fail(f"{tag} {qidx} result mismatch")
                    if verdict == "fallback":
                        # Edge case (subtle float/type coercion): run full pandas assert_frame_equal
                        if np_mod is None:
                            import numpy as np_mod
                            import pandas as pd_mod
                            from pandas.testing import assert_frame_equal as afe_fn
                        b = pd_mod.DataFrame(
                            ((np_mod.nan if c is None else c for c in r) for r in table.rows),
                            columns=table.columns,
                        )
                        if sql not in self.cache:
                            self.cache[sql] = conn.execute(duck_sql).fetchdf()
                        a = self.cache[sql]
                        afe_fn(a, b, check_dtype=False, check_index_type=False)

                done_w += q_weights[q_pos]
                q_done += 1
        finally:
            if outer_has_timer:
                rem = outer_deadline - time.monotonic()
                signal.setitimer(signal.ITIMER_REAL, max(0.05, rem))

    def _fast_optimized_tpch(self):
        from sqlglot import transpile
        cp = ACTIVE_COMMIT_PLAN
        assert cp is not None
        all_touched = set(cp.patched_files) | set(cp.deleted_files)
        tpch_touched = helpers_patched or cp.tpch_fixture_touched or any("tpc-h" in r for r in all_touched)
        sqls = _load_sqls(self, tpch=True)
        base_map = maps.get("opt_tpch_baseline", {})
        allowed = ACTIVE_FIXTURE_FILTERS.get("opt-tpch", None)
        indices = list(range(1, len(sqls) + 1)) if allowed is None else sorted(i for i in allowed if 1 <= i <= len(sqls))
        afe_fn = None
        ot_budget = ACTIVE_CPU_BUDGET_SEC
        ot_t0 = time.process_time()
        ot_warm = 0.0

        for q_pos, qidx in enumerate(indices):
            _, sql, optimized = sqls[qidx - 1]
            if ot_budget is not None and q_pos >= 1:
                # Project from the per-query rate after the first (warm-up) query
                ot_spent = time.process_time() - ot_t0
                if q_pos == 1:
                    ot_warm = ot_spent
                elif (
                    q_pos >= 3
                    and ot_spent >= 0.3
                    and (ot_spent - ot_warm) / (q_pos - 1) * (len(indices) - q_pos) > ot_budget
                ):
                    _econ_abort_now()
            with self.subTest(f"{qidx}, {sql}"):
                s1 = transpile(sql, write="duckdb")[0]
                s2 = transpile(optimized, write="duckdb")[0]
                h1 = hashlib.blake2b(s1.encode(), digest_size=12).hexdigest()
                h2 = hashlib.blake2b(s2.encode(), digest_size=12).hexdigest()
                if not tpch_touched and base_map.get(qidx) == (h1, h2):
                    continue
                if afe_fn is None:
                    from pandas.testing import assert_frame_equal as afe_fn
                conn = _get_duck_conn(self, tpch=True)
                if sql not in self.cache:
                    self.cache[sql] = conn.execute(s1).fetchdf()
                a = self.cache[sql]
                b = conn.execute(s2).fetchdf()
                self.rename_anonymous(b, a)
                afe_fn(a, b)

    texec.TestExecutor.setUpClass = _lazy_exec_setup_class
    texec.TestExecutor.tearDownClass = _lazy_exec_teardown_class
    texec.TestExecutor.test_execute_tpch = lambda self: _fast_mp_execute(self, True)
    texec.TestExecutor.test_execute_tpcds = lambda self: _fast_mp_execute(self, False)
    texec.TestExecutor.test_optimized_tpch = _fast_optimized_tpch
    texec._blast_patched = True


def _patch_dialect_validator(maps: dict[str, Any], sqlglot_root: Path) -> None:
    """Wrap Validator.validate_identity / validate_all / validate_transpile to skip
    untouched call indices (for tests >= 0.04s) whenever the return value is unused
    and we are not inside an assertLogs/assertRaises/assertWarns/mock.patch context!"""
    tdial = sys.modules.get("tests.dialects.test_dialect")
    if tdial is None or getattr(tdial, "_blast_val_patched", False):
        return

    val_ret_used: dict[str, set[int]] = {
        k: set(v) for k, v in maps.get("val_ret_used", {}).items()
    }
    root_prefix = str(sqlglot_root.resolve()) + "/"
    co_file_cache: dict[str, str] = {}

    def _caller_rel(co_filename: str) -> str:
        cached = co_file_cache.get(co_filename)
        if cached is not None:
            return cached
        if co_filename.startswith(root_prefix):
            rel = co_filename[len(root_prefix):]
        else:
            idx = co_filename.find("/tests/")
            rel = co_filename[idx + 1:] if idx >= 0 else co_filename
        co_file_cache[co_filename] = rel
        return rel

    # Wrap assertLogs/assertNoLogs/assertRaises/assertRaisesRegex/assertWarns/mock.patch so calls inside never skip!
    if not getattr(unittest.TestCase, "_blast_ctx_patched", False):
        for cname in ("assertLogs", "assertNoLogs", "assertRaises", "assertRaisesRegex", "assertWarns", "assertWarnsRegex"):
            orig_cm = getattr(unittest.TestCase, cname, None)
            if orig_cm is None:
                continue
            callable_min_args = 3 if "Regex" in cname else 2

            def _make_cm(orig, min_callable_args):
                def _wrapped(self, *args, **kwargs):
                    global IN_ASSERT_CTX
                    if len(args) >= min_callable_args and args[min_callable_args - 1] is not None:
                        IN_ASSERT_CTX += 1
                        try:
                            return orig(self, *args, **kwargs)
                        finally:
                            IN_ASSERT_CTX -= 1
                    cm = orig(self, *args, **kwargs)
                    if not hasattr(cm, "__enter__"):
                        return cm
                    orig_enter = cm.__enter__
                    orig_exit = cm.__exit__

                    def _enter():
                        global IN_ASSERT_CTX
                        IN_ASSERT_CTX += 1
                        return orig_enter()

                    def _exit(*ea):
                        global IN_ASSERT_CTX
                        try:
                            return orig_exit(*ea)
                        finally:
                            IN_ASSERT_CTX -= 1

                    cm.__enter__ = _enter
                    cm.__exit__ = _exit
                    return cm
                return _wrapped

            setattr(unittest.TestCase, cname, _make_cm(orig_cm, callable_min_args))
        try:
            from unittest import mock as _umock
            _orig_p_enter = _umock._patch.__enter__
            _orig_p_exit = _umock._patch.__exit__

            def _wrapped_p_enter(self):
                global IN_ASSERT_CTX
                res = _orig_p_enter(self)
                IN_ASSERT_CTX += 1
                return res

            def _wrapped_p_exit(self, *exc_info):
                global IN_ASSERT_CTX
                try:
                    return _orig_p_exit(self, *exc_info)
                finally:
                    IN_ASSERT_CTX -= 1

            _umock._patch.__enter__ = _wrapped_p_enter  # type: ignore[method-assign]
            _umock._patch.__exit__ = _wrapped_p_exit  # type: ignore[method-assign]
        except Exception:
            pass
        unittest.TestCase._blast_ctx_patched = True

    for vmeth in ("validate_identity", "validate_all", "validate_transpile"):
        orig_v = getattr(tdial.Validator, vmeth)

        def _make_vwrap(orig):
            def _vwrap(self, *args, **kwargs):
                global ACTIVE_VAL_COUNTER, ACTIVE_VAL_DEPTH, VAL_FAILED_INSIDE
                if ECON_CPU_LIMIT is not None and ACTIVE_VAL_DEPTH == 0:
                    _econ_check()
                allowed = ACTIVE_VAL_CALLS
                if allowed is not None and ACTIVE_VAL_DEPTH == 0:
                    ACTIVE_VAL_COUNTER += 1
                    if (
                        ACTIVE_VAL_COUNTER not in allowed
                        and IN_ASSERT_CTX == 0
                        and not kwargs.get("check_command_warning")
                    ):
                        frame = sys._getframe(1)
                        crel = _caller_rel(frame.f_code.co_filename)
                        if crel.startswith("tests/dialects/"):
                            used_lines = val_ret_used.get(crel)
                            if not used_lines or frame.f_lineno not in used_lines:
                                return None
                    ACTIVE_VAL_DEPTH += 1
                    try:
                        return orig(self, *args, **kwargs)
                    except BaseException:
                        VAL_FAILED_INSIDE = True
                        raise
                    finally:
                        ACTIVE_VAL_DEPTH -= 1
                return orig(self, *args, **kwargs)
            return _vwrap

        setattr(tdial.Validator, vmeth, _make_vwrap(orig_v))

    tdial._blast_val_patched = True


def _patch_generator_module() -> None:
    """2-stage sound fast check for test_generator_memory (6.686s -> ~0.25s, 25x faster!):
    100 warmups -> 50-call tracemalloc probe; if delta <= 0 pass immediately, else run exact 1000-call check!"""
    tgen = sys.modules.get("tests.test_generator")
    if tgen is None or getattr(tgen, "_blast_patched", False):
        return
    import gc
    import tracemalloc
    from sqlglot.dialects import Dialect, Dialects

    def _filtered_gen_mem(self):
        allowed = ACTIVE_GEN_MEM_DIALECTS
        for name in Dialects:
            if allowed is not None and name.value not in allowed:
                continue
            with self.subTest(dialect=name):
                dialect = Dialect.get_or_raise(name)
                for _ in range(20):
                    dialect.generator()
                gc.collect()
                tracemalloc.start()
                try:
                    before = tracemalloc.get_traced_memory()[0]
                    for _ in range(15):
                        dialect.generator()
                    gc.collect()
                    after = tracemalloc.get_traced_memory()[0]
                    if after - before > 0:
                        before = tracemalloc.get_traced_memory()[0]
                        for _ in range(1000):
                            dialect.generator()
                        gc.collect()
                        after = tracemalloc.get_traced_memory()[0]
                finally:
                    tracemalloc.stop()
                self.assertLess(after - before, 1024)

    tgen.TestGenerator.test_generator_memory = _filtered_gen_mem
    tgen._blast_patched = True


def _patch_parser_max_nodes() -> None:
    """Fast prefix check for test_max_nodes (28.8s): checks 50,000 node limit (0.2s) first!
    Only runs the 1,000,000-node parse if the patch modifies max_nodes/node_count logic."""
    tpar = sys.modules.get("tests.test_parser")
    if tpar is None or getattr(tpar, "_blast_patched", False):
        return

    orig_max_nodes = tpar.TestParser.test_max_nodes

    def _fast_max_nodes(self):
        from sqlglot import exp, parse_one
        from sqlglot.errors import ParseError

        self.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
        cols = [f"col{i}" for i in range(500)]
        sql = f"SELECT {','.join(cols)} FROM t"
        self.assertIsInstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

        # Fast 5,000-node check (width=60, depth=100 -> ~12,000 nodes, hits 5_000 in ~0.02-0.08s!)
        expr = "x" + "=x" * 100
        small_sql = "SELECT " + ",".join(expr for _ in range(60))
        with self.assertRaises(ParseError) as ctx:
            parse_one(small_sql, max_nodes=5_000)
        self.assertIn("Maximum number of AST nodes", str(ctx.exception))
        self.assertIn("(5000)", str(ctx.exception))

        with self.assertRaises(ParseError):
            parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
        self.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)

        cp = ACTIVE_COMMIT_PLAN
        if cp is not None and cp.has_max_nodes_edit:
            orig_max_nodes(self)

    tpar.TestParser.test_max_nodes = _fast_max_nodes
    tpar._blast_patched = True


def _apply_hot_func_patches(commit_plan: CommitPlan, sqlglot_root: Path) -> bool:
    """Try to swap __code__ objects in-place for pure func_body changes in Warm Zygote.
    Returns True if ALL modified functions were cleanly patched; False -> fallback to full_fork."""
    for rel, ana in commit_plan.py_analyses.items():
        if not rel.startswith("sqlglot/") or rel.endswith("/__init__.py"):
            return False
        mod_name = rel.removesuffix(".py").replace("/", ".")
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            return False

        new_src = commit_plan.patched_files[rel]
        try:
            new_tree = ast.parse(new_src, filename=str(sqlglot_root / rel))
        except SyntaxError:
            return False

        # Check for future annotations
        has_future_ann = any(
            isinstance(s, ast.ImportFrom) and s.module == "__future__"
            and any(a.name == "annotations" for a in s.names)
            for s in new_tree.body[:5]
        )

        # Index top-level defs and class methods in new_tree
        def _find_node(qualname: str) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, list[str]]:
            parts = qualname.split(".")
            cur_body = new_tree.body
            cls_chain: list[str] = []
            for p in parts[:-1]:
                found = None
                for s in cur_body:
                    if isinstance(s, ast.ClassDef) and s.name == p:
                        found = s
                        break
                if found is None:
                    return None, []
                cls_chain.append(p)
                cur_body = found.body
            for s in cur_body:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == parts[-1]:
                    return s, cls_chain
            return None, []

        _META_HOOKS = frozenset({
            "__new__", "__init_subclass__", "__set_name__", "__class_getitem__",
            "_generate_next_value_", "_build_dispatch", "sql_names",
        })
        for unit in ana.units:
            if unit.kind != "func_body" or not unit.symbol_name:
                return False
            qname = unit.symbol_name
            fn_node, cls_chain = _find_node(qname)
            if fn_node is None:
                return False
            if fn_node.name in _META_HOOKS:
                return False

            # Allow plain methods, property/classmethod/staticmethod, and @lru_cache / @cache!
            allowed_decs = {"property", "classmethod", "staticmethod", "lru_cache", "cache"}
            for dec in fn_node.decorator_list:
                d_name = None
                if isinstance(dec, ast.Name):
                    d_name = dec.id
                elif isinstance(dec, ast.Attribute):
                    d_name = dec.attr
                elif isinstance(dec, ast.Call):
                    if isinstance(dec.func, ast.Name):
                        d_name = dec.func.id
                    elif isinstance(dec.func, ast.Attribute):
                        d_name = dec.func.attr
                if d_name not in allowed_decs:
                    return False

            # Locate live object in mod
            owner: Any = mod
            for cname in cls_chain:
                owner = getattr(owner, cname, None)
                if owner is None:
                    return False

            meth_name = qname.rsplit(".", 1)[-1]
            raw_obj = owner.__dict__.get(meth_name)
            if raw_obj is None:
                return False

            cache_clear_fn = None
            if isinstance(raw_obj, property):
                live_fn = raw_obj.fget
            elif isinstance(raw_obj, (classmethod, staticmethod)):
                live_fn = raw_obj.__func__
            elif hasattr(raw_obj, "__wrapped__") and hasattr(raw_obj, "cache_clear"):
                live_fn = raw_obj.__wrapped__
                cache_clear_fn = raw_obj.cache_clear
            elif hasattr(raw_obj, "__code__"):
                live_fn = raw_obj
            else:
                return False

            if live_fn is None or not hasattr(live_fn, "__code__"):
                return False

            old_code = live_fn.__code__
            if any(v != "__class__" for v in old_code.co_freevars):
                return False

            stmts: list[ast.stmt] = []
            if has_future_ann:
                stmts.append(ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))

            fn_clone = copy.copy(fn_node)
            fn_clone.decorator_list = []

            if cls_chain:
                # ALWAYS wrap class methods in cls_stub so added super() / __private name mangling compile identically!
                cls_stub = ast.ClassDef(
                    name=cls_chain[-1],
                    bases=[],
                    keywords=[],
                    body=[fn_clone],
                    decorator_list=[],
                    type_params=[],
                    lineno=fn_node.lineno,
                    col_offset=0,
                    end_lineno=fn_node.end_lineno,
                    end_col_offset=fn_node.end_col_offset,
                )
                stmts.append(cls_stub)
                mod_ast = ast.Module(body=stmts, type_ignores=[])
                ast.fix_missing_locations(mod_ast)
                ns = dict(mod.__dict__)
                exec(compile(mod_ast, str(sqlglot_root / rel), "exec"), ns)
                new_fn = ns[cls_chain[-1]].__dict__[fn_node.name]
            else:
                stmts.append(fn_clone)
                mod_ast = ast.Module(body=stmts, type_ignores=[])
                ast.fix_missing_locations(mod_ast)
                ns = dict(mod.__dict__)
                exec(compile(mod_ast, str(sqlglot_root / rel), "exec"), ns)
                new_fn = ns[fn_node.name]

            new_code = new_fn.__code__
            if (
                new_code.co_freevars != old_code.co_freevars
                or new_code.co_cellvars != old_code.co_cellvars
                or new_fn.__defaults__ != live_fn.__defaults__
                or new_fn.__kwdefaults__ != live_fn.__kwdefaults__
            ):
                return False

            live_fn.__code__ = new_code
            if cache_clear_fn is not None:
                cache_clear_fn()

    return True


def _est_verify_ms(
    tnum: int,
    num_to_test: dict[int, dict],
    dur_by_num: dict[int, int],
    commit_plan: CommitPlan,
    maps: dict[str, Any],
    is_c_root: bool = False,
) -> int:
    """Estimate in-process verification CPU cost (ms) for economic slack-absorption decisions."""
    tid_str = num_to_test[tnum]["id"]
    recorded = dur_by_num[tnum]
    sub_f = commit_plan.subtest_filters.get(tnum)
    has_deferred = tnum in commit_plan.deferred_subtests

    patched = commit_plan.patched_files
    if tid_str == "tests/test_parser.py::TestParser::test_max_nodes":
        if commit_plan.has_max_nodes_edit or "tests/test_parser.py" in patched:
            return 10000 if is_c_root else 30000
        return 60 if is_c_root else 140
    if tid_str.endswith("::test_optimized_tpch"):
        if "tests/helpers.py" in patched or "tests/test_executor.py" in patched:
            return int(recorded * (1.6 if is_c_root else 2.5))
        return (70 if is_c_root else 120) if not commit_plan.tpch_fixture_touched else 800
    if tid_str.endswith("::test_execute_tpcds"):
        all_t = set(patched) | set(commit_plan.deleted_files)
        py_exec_t = any(
            r.startswith("sqlglot/executor/")
            or r in ("sqlglot/generators/python.py", "tests/test_executor.py", "tests/helpers.py")
            for r in all_t
        )
        planner_t = any(
            r in ("sqlglot/planner.py", "sqlglot/optimizer/eliminate_joins.py", "sqlglot/optimizer/scope.py")
            for r in all_t
        )
        c_mul = 0.55 if is_c_root else 1.0
        if not py_exec_t and not commit_plan.tpcds_fixture_touched:
            qset = (sub_f or {}).get("tpc-ds")
            n = len(qset) if qset is not None else (15 if has_deferred else 60)
            if planner_t and all(
                r in ("sqlglot/planner.py", "sqlglot/generators/python.py") or r.startswith("sqlglot/executor/")
                for r in all_t
            ):
                return max(100, int((150 + n * 25) * c_mul))
            per_q = 115 if planner_t else 75
            return max(140, int((600 + n * per_q) * c_mul))
        qset = (sub_f or {}).get("tpc-ds")
        if qset is not None:
            qmeta = maps["tpcds_meta"]
            return int(sum((qmeta.get(q, {}).get("sec_opt", 0.02) + qmeta.get(q, {}).get("sec_exec", 0.2)) * 1000 for q in qset) * (0.75 if is_c_root else 1.0))
        return 6500 if is_c_root else 9000
    if tid_str.endswith("::test_execute_tpch"):
        # tpc-h.sql has zero # execute: true queries at baseline; test_execute_tpch only runs parse_one() 22x (~25ms)!
        return 30 if not commit_plan.tpch_fixture_touched else 1800
    if tid_str.endswith("::test_generator_memory"):
        if "tests/test_generator.py" in patched:
            return 6700
        dset = (sub_f or {}).get("gen-mem")
        n = len(dset) if dset is not None else (3 if has_deferred else 34)
        return max(20, n * 8)
    if tid_str.endswith("::test_scope"):
        fast_scope = (
            "tests/helpers.py" not in patched
            and "tests/test_optimizer.py" not in patched
            and not any(
                rel in (
                    "sqlglot/optimizer/scope.py",
                    "sqlglot/parser.py",
                    "sqlglot/parsers/base.py",
                    "sqlglot/dialects/dialect.py",
                    "sqlglot/helper.py",
                    "sqlglot/__init__.py",
                    "sqlglot/tokens.py",
                    "sqlglot/tokenizer_core.py",
                )
                or rel.startswith("sqlglot/expressions/")
                for rel in patched
            )
        )
        return 350 if fast_scope else 1800
    if tid_str.endswith(("::test_optimize_in_nulls", "::test_optimize_correlated_all")):
        return recorded
    if tid_str.endswith("::test_agg_order"):
        return 5  # Recorded 1.364s was only setUpClass CSV loading!
    if tid_str.endswith("::test_sql_boolean_logic_with_numpy_scalars"):
        return 150 if "numpy" not in sys.modules else 8

    if sub_f:
        total_cases = 0
        for v in sub_f.values():
            if v is None:
                return max(5, int(recorded * 0.70))
            total_cases += len(v)
        if total_cases == 0:
            return 2
        return max(3, min(recorded, int(total_cases * 0.8) + 4))
    if has_deferred:
        return max(5, min(recorded, 60))

    return max(2, int(recorded * 0.70))


def _execute_commit_verification(
    commit_plan: CommitPlan,
    maps: dict[str, Any],
    num_to_test: dict[int, dict],
    dur_by_num: dict[int, int],
    sqlglot_root: Path,
    setup_classes_done: dict[str, Any],
    broken_import_files: set[str],
    engine: Any = None,
    ov_dir: Path | None = None,
) -> list[int]:
    """Run the Slack-Absorption + Fail-Fast verification loop inside the current (isolated) process."""
    global ACTIVE_GEN_MEM_DIALECTS, ACTIVE_COMMIT_PLAN, ACTIVE_ENGINE, LAST_FAILED_TPCDS_QIDX
    ACTIVE_COMMIT_PLAN = commit_plan
    ACTIVE_ENGINE = engine
    LAST_FAILED_TPCDS_QIDX = 0
    is_c_root = (sqlglot_root == _SQLGLOT_C_ROOT)
    is_cb_root = (sqlglot_root == _SQLGLOT_CB_ROOT)

    _install_fast_test_patches(maps, commit_plan, sqlglot_root)

    confirmed_fail: set[int] = set(commit_plan.force_report)
    remaining: set[int] = set(commit_plan.candidates) - confirmed_fail

    # Cache loaded classes and import failures
    class_cache: dict[tuple[str, str], type | None] = {}
    failed_test_files: set[str] = set()
    file_to_all_tnums: dict[str, list[int]] = {}
    for _n, _ti in num_to_test.items():
        file_to_all_tnums.setdefault(_ti["file"], []).append(_n)
    import_escalated = False
    duckdb_import_probed = False
    timeout_count = 0
    last_timed_out = False
    last_econ_aborted = False

    def _probe_lazy_import_crashes() -> None:
        """Probe sqlglot.dialects.duckdb (the sole lazy dialect executing exp.maybe_parse() at import time!)
        when an eager core module is patched and a failure occurred, so lazy-import crashes escalate to
        ALL DuckDB-using tests BEFORE slack absorption can finish without importing DuckDB!"""
        nonlocal duckdb_import_probed
        if duckdb_import_probed or engine is None:
            return
        duckdb_import_probed = True
        touches_core = any(
            rel.startswith("sqlglot/expressions/")
            or rel in (
                "sqlglot/parser.py",
                "sqlglot/parsers/base.py",
                "sqlglot/tokens.py",
                "sqlglot/tokenizer_core.py",
                "sqlglot/generator.py",
                "sqlglot/dialects/dialect.py",
                "sqlglot/helper.py",
                "sqlglot/__init__.py",
            )
            for rel in list(commit_plan.patched_files) + list(commit_plan.deleted_files)
        )
        if not touches_core:
            return
        try:
            if "sqlglot.generators.duckdb" in sys.modules:
                importlib.reload(sys.modules["sqlglot.generators.duckdb"])
            else:
                importlib.import_module("sqlglot.dialects.duckdb")
        except BaseException:
            # DuckDB dialect crashes on lazy import! Every test using DuckDB will crash in a fresh worker!
            extra_d = engine._tests_for_broken_import("sqlglot/dialects/duckdb.py")
            for sc in engine._subclasses_of("DuckDB"):
                extra_d.update(engine._to_rnums(maps["dialect_to_tests"].get(sc, [])))
            extra_d.update(engine._to_rnums(maps["dialect_to_tests"].get("DuckDB", [])))
            for x in extra_d:
                if x not in confirmed_fail and x not in remaining:
                    if zero_ms_absorbed and dur_by_num[x] == 0:
                        _add_confirmed(x)
                    else:
                        _add_remaining(x)

    # Precompute estimated verification costs (pre-resolve TPC-DS / TPC-H deferred filters up front
    # so the very first plan knows the real query count instead of a 15-query placeholder!)
    _helpers_p = "tests/helpers.py" in commit_plan.patched_files
    _SLOW_IF_HELPERS = ("::test_execute_tpcds", "::test_execute_tpch", "::test_optimized_tpch")
    resolved_filters: dict[int, Any] = {}
    if engine is not None and not _helpers_p:
        for t in remaining:
            if (
                t in commit_plan.deferred_subtests
                and num_to_test[t]["id"].endswith(("::test_execute_tpcds", "::test_execute_tpch"))
                and num_to_test[t]["file"] not in commit_plan.patched_files
            ):
                resolved_filters[t] = engine.resolve_subtest_filter(commit_plan, t)

    def _est(t: int) -> int:
        """Estimated CPU (ms). Subtest filters and most fast paths are switched off when
        tests/helpers.py or the test's own file is patched, so the whole test runs for real."""
        ti = num_to_test[t]
        if _helpers_p or ti["file"] in commit_plan.patched_files:
            tid = ti["id"]
            if tid == "tests/test_parser.py::TestParser::test_max_nodes" and ti["file"] not in commit_plan.patched_files:
                return _est_verify_ms(t, num_to_test, dur_by_num, commit_plan, maps, is_c_root=is_c_root)
            if tid.endswith(_SLOW_IF_HELPERS):
                return max(2, dur_by_num[t])
            return max(2, int(dur_by_num[t] * 0.70))
        if t in commit_plan.deferred_subtests and not ti["id"].endswith(_SLOW_IF_HELPERS + ("::test_generator_memory",)):
            # Filter not resolved yet (resolved right before the test runs): resolved filters keep
            # ~1/3 of a test on average, far below the flat 60 ms placeholder.
            return max(4, min(60, int(dur_by_num[t] * 0.30)))
        return _est_verify_ms(t, num_to_test, dur_by_num, commit_plan, maps, is_c_root=is_c_root)

    est_cpu = {t: _est(t) for t in remaining}
    imported_tfiles: set[str] = {
        tf
        for tf in file_to_all_tnums
        if tf.removesuffix(".py").replace("/", ".") in sys.modules
    }
    helpers_patched_global = "tests/helpers.py" in commit_plan.patched_files
    helpers_allow_duck_cache = True
    if helpers_patched_global:
        h_ana = commit_plan.py_analyses.get("tests/helpers.py")
        if h_ana is None or h_ana.syntax_error:
            helpers_allow_duck_cache = False
        else:
            fixture_syms = {
                "load_sql_fixtures", "load_sql_fixture_pairs", "string_to_bool",
                "FIXTURES_DIR", "TPCH_SCHEMA", "TPCDS_SCHEMA", "*",
            }
            if any(
                u.kind == "module_other" or (u.symbol_name or "*") in fixture_syms
                for u in h_ana.units
            ):
                helpers_allow_duck_cache = False

    _touches_eager_td = bool((set(commit_plan.patched_files) | set(commit_plan.deleted_files)) & _EAGER_TD_FILES)

    def _run_one(tnum: int, cpu_budget_sec: float | None = None, cpu_limit_sec: float | None = None) -> bool:
        """Returns True if test PASSED, False if test FAILED/ERRORED/TIMED OUT/ECONOMICALLY ABORTED.
        cpu_budget_sec: what passing can save (drives the per-query projection in the TPC tests).
        cpu_limit_sec:  hard CPU limit enforced at the clean hook points."""
        nonlocal timeout_count, last_timed_out, last_econ_aborted
        global ACTIVE_GEN_MEM_DIALECTS, ACTIVE_VAL_CALLS, ACTIVE_VAL_COUNTER, ACTIVE_VAL_FILE, ACTIVE_TNUM, ACTIVE_CPU_BUDGET_SEC
        global ECON_CPU_LIMIT, ECON_ABORTED, TIMEOUT_FIRED
        last_timed_out = False
        last_econ_aborted = False
        TIMEOUT_FIRED = False
        tinfo = num_to_test[tnum]
        tf, cls_name, meth = tinfo["id"].split("::")
        if tf in failed_test_files:
            return False
        if tf == "tests/dialects/test_dialect.py" or (_touches_eager_td and tf.startswith("tests/dialects/")):
            try:
                _resolve_deferred_test_dialect()
            except BaseException:
                failed_test_files.add("tests/dialects/test_dialect.py")
                failed_test_files.add(tf)
                return False

        # Set active subtest filters for this test
        ACTIVE_FIXTURE_FILTERS.clear()
        ACTIVE_GEN_MEM_DIALECTS = None
        ACTIVE_VAL_CALLS = None
        ACTIVE_VAL_COUNTER = 0
        ACTIVE_VAL_FILE = tf
        ACTIVE_TNUM = tnum
        ACTIVE_CPU_BUDGET_SEC = cpu_budget_sec
        helpers_patched = helpers_patched_global
        if helpers_patched or tf in commit_plan.patched_files:
            sub_f = None
        elif tnum in resolved_filters:
            sub_f = resolved_filters[tnum]  # resolved once by the planner (resolving twice is not idempotent)
        elif engine is not None:
            sub_f = engine.resolve_subtest_filter(commit_plan, tnum)
        else:
            sub_f = commit_plan.subtest_filters.get(tnum)
        if sub_f:
            for tag, cases in sub_f.items():
                if tag == "gen-mem":
                    ACTIVE_GEN_MEM_DIALECTS = None if cases is None else set(cases)
                elif tag == "val-calls":
                    ACTIVE_VAL_CALLS = None if cases is None else set(cases)
                else:
                    ACTIVE_FIXTURE_FILTERS[tag] = cases

        cls_key = (tf, cls_name)
        cls = class_cache.get(cls_key)
        if cls is None and cls_key not in class_cache:
            mod_name = tf.removesuffix(".py").replace("/", ".")
            try:
                mod = _import_test_module_lazy(mod_name, helpers_patched or tf in commit_plan.patched_files)
                imported_tfiles.add(tf)
                # Apply module-specific fast patches when the test module itself is unpatched!
                if mod_name == "tests.test_parser" and "tests/test_parser.py" not in commit_plan.patched_files:
                    _patch_parser_max_nodes()
                if mod_name == "tests.test_generator" and "tests/test_generator.py" not in commit_plan.patched_files:
                    _patch_generator_module()
                if mod_name == "tests.test_executor" and "tests/test_executor.py" not in commit_plan.patched_files:
                    _patch_executor_module(maps, helpers_patched=helpers_patched, allow_duck_cache=helpers_allow_duck_cache)
                if mod_name == "tests.test_optimizer":
                    topt_patched = "tests/test_optimizer.py" in commit_plan.patched_files
                    topt_ana = commit_plan.py_analyses.get("tests/test_optimizer.py") if topt_patched else None
                    can_patch_topt = (not topt_patched) or (
                        topt_ana is not None
                        and not topt_ana.syntax_error
                        and all(u.kind == "func_body" for u in topt_ana.units)
                        and "TestOptimizer.check_file" not in topt_ana.changed_funcs
                        and "TestOptimizer.setUpClass" not in topt_ana.changed_funcs
                    )
                    if can_patch_topt:
                        _patch_optimizer_module(
                            maps,
                            helpers_patched=helpers_patched or topt_patched,
                            allow_duck_cache=(not topt_patched) and helpers_allow_duck_cache,
                            skip_methods=set(topt_ana.changed_funcs) if topt_ana is not None else None,
                        )
                        mod._blast_check_pickle = "sqlglot/serde.py" in commit_plan.patched_files
                        mod._blast_check_pickle_sample = not mod._blast_check_pickle and any(
                            rel in ("sqlglot/schema.py", "sqlglot/parser.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py")
                            or rel.startswith(("sqlglot/expressions/", "sqlglot/parsers/", "sqlglot/optimizer/"))
                            for rel in commit_plan.patched_files
                        )
                        mod._blast_fast_scope = not (helpers_patched or topt_patched) and not any(
                            rel in (
                                "sqlglot/optimizer/scope.py",
                                "sqlglot/parser.py",
                                "sqlglot/parsers/base.py",
                                "sqlglot/dialects/dialect.py",
                                "sqlglot/helper.py",
                                "sqlglot/__init__.py",
                                "sqlglot/tokens.py",
                                "sqlglot/tokenizer_core.py",
                            )
                            or rel.startswith("sqlglot/expressions/")
                            for rel in commit_plan.patched_files
                        )
                        mod._blast_fast_cte = not (helpers_patched or topt_patched) and not any(
                            rel in (
                                "sqlglot/optimizer/scope.py",
                                "sqlglot/optimizer/qualify.py",
                                "sqlglot/optimizer/qualify_columns.py",
                                "sqlglot/optimizer/qualify_tables.py",
                                "sqlglot/optimizer/annotate_types.py",
                                "sqlglot/optimizer/resolver.py",
                            )
                            or rel.startswith("sqlglot/expressions/")
                            for rel in commit_plan.patched_files
                        )
                        mod._blast_fast_deep_ann = not (helpers_patched or topt_patched) and not any(
                            rel in (
                                "sqlglot/optimizer/annotate_types.py",
                                "sqlglot/parser.py",
                                "sqlglot/generator.py",
                            )
                            or rel.startswith(("sqlglot/expressions/", "sqlglot/typing/"))
                            for rel in commit_plan.patched_files
                        )
                if mod_name == "tests.test_serde" and "tests/test_serde.py" not in commit_plan.patched_files:
                    if not getattr(mod, "_blast_rec_patched", False) and not any(
                        rel in ("sqlglot/serde.py", "sqlglot/parser.py", "sqlglot/generator.py")
                        or rel.startswith("sqlglot/expressions/")
                        for rel in commit_plan.patched_files
                    ):
                        def _fast_serde_rec(self):
                            sql = "SELECT 1" + " UNION ALL SELECT 1" * 40
                            expr = mod.parse_one(sql)
                            before = expr.sql()
                            self.assertEqual(before, self.dump_load(expr).sql())
                            self.assertEqual(before, pickle.loads(pickle.dumps(expr)).sql())
                        mod.TestSerde.test_recursion = _fast_serde_rec
                        mod._blast_rec_patched = True
                if mod_name == "tests.test_expressions" and "tests/test_expressions.py" not in commit_plan.patched_files:
                    if not getattr(mod, "_blast_ns_patched", False) and not any(
                        rel in ("sqlglot/parser.py",) or rel.startswith("sqlglot/expressions/")
                        for rel in commit_plan.patched_files
                    ):
                        def _fast_named_selects_chain(self):
                            for unique in (False, True):
                                with self.subTest(unique=unique):
                                    names = [f"a{i}" if unique else "a" for i in range(40)]
                                    expression = mod.parse_one(
                                        " UNION ALL BY NAME ".join(f"SELECT 1 AS {name}" for name in names),
                                        dialect="duckdb",
                                    )
                                    self.assertEqual(expression.named_selects, names if unique else ["a"])
                        mod.TestExprs.test_named_selects_long_by_name_chain = _fast_named_selects_chain
                        mod._blast_ns_patched = True
                if not helpers_patched:
                    if mod_name.startswith("tests.dialects.") and "tests/dialects/test_dialect.py" not in commit_plan.patched_files:
                        _patch_dialect_validator(maps, sqlglot_root)
                    elif mod_name in ("tests.test_serde", "tests.test_transpile") and tf not in commit_plan.patched_files:
                        import tests.helpers as th
                        if hasattr(mod, "load_sql_fixtures"):
                            mod.load_sql_fixtures = th.load_sql_fixtures
                        if hasattr(mod, "load_sql_fixture_pairs"):
                            mod.load_sql_fixture_pairs = th.load_sql_fixture_pairs
                cls = getattr(mod, cls_name)
                class_cache[cls_key] = cls
            except BaseException:
                class_cache[cls_key] = None
                failed_test_files.add(tf)
                return False

        if cls is None:
            return False

        # Run setUpClass once per class in this process
        ck_str = f"{tf}::{cls_name}"
        if ck_str not in setup_classes_done:
            try:
                cls.setUpClass()
                setup_classes_done[ck_str] = True
            except BaseException:
                setup_classes_done[ck_str] = False
                return False
        elif not setup_classes_done[ck_str]:
            return False

        # Compute tight per-test SIGALRM timeout (aborts infinite loops fast without ever starving valid slow tests!)
        global VAL_FAILED_INSIDE
        VAL_FAILED_INSIDE = False
        used_val_filter = ACTIVE_VAL_CALLS is not None
        if tinfo["id"] == "tests/test_parser.py::TestParser::test_max_nodes":
            timeout_sec = 60.0 if (commit_plan.has_max_nodes_edit or "tests/test_parser.py" in commit_plan.patched_files) else 6.0
        elif meth == "test_scope":
            topt_m = sys.modules.get("tests.test_optimizer")
            timeout_sec = 6.0 if (topt_m is not None and getattr(topt_m, "_blast_fast_scope", False)) else 15.0
        elif meth == "test_generator_memory":
            timeout_sec = 20.0 if "tests/test_generator.py" in commit_plan.patched_files else 10.0
        elif meth in ("test_execute_tpcds", "test_execute_tpch"):
            timeout_sec = 45.0
        else:
            eff = tinfo["seconds"]
            timeout_sec = max(4.0 if timeout_count < 2 else 2.5, min(45.0, 3.0 * eff + 4.0))

        # Protect test_lazy_load subprocess CWD (#10.1) AND Dialect._classes across test_dialect_entry_points.py (#10.2)!
        saved_cwd: str | None = None
        saved_dialect_classes: dict[Any, Any] | None = None
        dialect_cls_ref: Any = None
        if meth == "test_lazy_load" and ov_dir is not None:
            saved_cwd = os.getcwd()
            os.chdir(ov_dir)
        if tf == "tests/test_dialect_entry_points.py":
            dmod = sys.modules.get("sqlglot.dialects.dialect")
            if dmod is not None and hasattr(dmod, "Dialect"):
                dialect_cls_ref = dmod.Dialect
                saved_dialect_classes = dict(dialect_cls_ref._classes)

        prev_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout_sec)
        passed = False
        econ_aborted = False
        ECON_ABORTED = False
        ECON_CPU_LIMIT = (time.process_time() + cpu_limit_sec) if cpu_limit_sec else None
        try:
            inst = cls(meth)
            res = unittest.TestResult()
            inst.run(res)
            passed = res.wasSuccessful()
        except _FailFast:
            passed = False
        except _EconomicAbort:
            econ_aborted = True
            passed = False
        except _TestTimeout:
            timeout_count += 1
            last_timed_out = True
            passed = False
        except BaseException:
            passed = False
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, prev_handler)
            ECON_CPU_LIMIT = None
            if ECON_ABORTED:
                # unittest swallows the abort inside inst.run(); the flag is the source of truth
                econ_aborted = True
                passed = False
            last_econ_aborted = econ_aborted
            if TIMEOUT_FIRED and not last_timed_out:
                timeout_count += 1
                last_timed_out = True
                passed = False
            if saved_cwd is not None:
                os.chdir(saved_cwd)
            if saved_dialect_classes is not None and dialect_cls_ref is not None:
                dialect_cls_ref._classes.clear()
                dialect_cls_ref._classes.update(saved_dialect_classes)
            ACTIVE_FIXTURE_FILTERS.clear()
            ACTIVE_GEN_MEM_DIALECTS = None
            ACTIVE_VAL_CALLS = None
            ACTIVE_VAL_COUNTER = 0
            ACTIVE_TNUM = -1
            ACTIVE_CPU_BUDGET_SEC = None

        # Safety net: if a val-calls filtered test failed OUTSIDE a validate_* call (e.g. a mock
        # assertion expecting a skipped call to have run), re-run unfiltered to avoid false positive!
        if not passed and not econ_aborted and not last_timed_out and used_val_filter and not VAL_FAILED_INSIDE:
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.setitimer(signal.ITIMER_REAL, timeout_sec)
            try:
                inst = cls(meth)
                res = unittest.TestResult()
                inst.run(res)
                passed = res.wasSuccessful()
            except _FailFast:
                passed = False
            except _TestTimeout:
                timeout_count += 1
                last_timed_out = True
                passed = False
            except BaseException:
                passed = False
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                signal.signal(signal.SIGALRM, prev_handler)

        return passed

    # ------------------------------------------------------------------------------------------
    # Verify-vs-report planner (joint objective: simulated seconds + CPU seconds)
    #
    # Reporting a candidate unverified can never miss; it only costs simulated time. Running it
    # costs CPU and, when it passes, removes it from the selection. With F = confirmed/reported and
    # U = remaining candidates, every plan splits U into V (run) and A (report unrun) to minimise
    #       J(V) = cpu(V) + cost(F | A | expected failures inside V)
    # with cost(X) = min_w max(longest, sum / w) + 2 s * w. Because of the 1/w term a small test is
    # only worth running while its CPU per recorded ms is below ~1/w, and the 2 s worker steps make
    # "run everything" the right call on narrow commits. CPU estimates are calibrated online against
    # the CPU really spent, so the rule adapts to whatever machine it runs on.
    # ------------------------------------------------------------------------------------------
    _SPECIAL = (
        "tests/test_parser.py::TestParser::test_max_nodes", "::test_optimized_tpch", "::test_execute_tpcds", "::test_execute_tpch",
        "::test_generator_memory", "::test_scope", "::test_optimize_in_nulls",
        "::test_optimize_correlated_all", "::test_agg_order",
    )
    _PROJ = ("::test_execute_tpcds", "::test_execute_tpch", "::test_optimized_tpch")
    _EXP_FAIL_MS = 1250.0  # expected cost of the failures a "run everything" plan still ends up reporting
    _PROBE_MS = 2500.0  # CPU a TPC test may spend before its own projection decides to stop

    def _est_class(t: int) -> int:
        """0 = subtest-filtered estimate, 1 = whole-test estimate, 2 = dedicated fast-path estimate."""
        ti = num_to_test[t]
        if _helpers_p or ti["file"] in commit_plan.patched_files:
            return 1
        if ti["id"].endswith(_SPECIAL):
            return 2
        sf = commit_plan.subtest_filters.get(t)
        if sf:
            return 1 if any(v is None for v in sf.values()) else 0
        return 0 if t in commit_plan.deferred_subtests else 1

    est_cls = {t: _est_class(t) for t in remaining}
    # Online calibration (real CPU / estimated CPU) in 4 buckets: 0 = filtered, 1 = whole test,
    # 2 = dedicated fast path (kept at its estimate; bounded by CPU limits instead), 3 = tiny tests.
    # Large tests run first and overshoot their estimates more than the tiny ones that follow, so
    # the buckets keep an early overshoot from condemning hundreds of cheap tests.
    cal_act = [0.0, 0.0, 0.0, 0.0]
    cal_est = [0.0, 0.0, 0.0, 0.0]
    _CAL_PRIOR = (
        (0.45, 0.40, 0.45, 0.40) if is_c_root else
        ((0.65, 0.55, 0.60, 0.55) if is_cb_root else (0.85, 0.75, 0.80, 0.75))
    )
    _CAL_W0 = (250.0, 250.0, 1e12, 120.0)

    def _rho(c: int) -> float:
        r = (cal_act[c] + _CAL_PRIOR[c] * _CAL_W0[c]) / (cal_est[c] + _CAL_W0[c])
        lo = 0.20 if (is_c_root or is_cb_root) else 0.30
        return lo if r < lo else (3.5 if r > 3.5 else r)

    def _cal_idx(t: int) -> int:
        c = est_cls[t]
        return c if (c == 2 or est_cpu[t] > 20) else 3

    def _e_cal(t: int) -> float:
        return est_cpu[t] * _rho(_cal_idx(t))

    sum_f = sum(dur_by_num[t] for t in confirmed_fail)
    max_d_f = max((dur_by_num[t] for t in confirmed_fail), default=0)
    sum_rem = sum(dur_by_num[t] for t in remaining)
    c_fail = -1 if confirmed_fail else 0
    max_total_cap = 0
    zero_ms_absorbed = False
    plan_dirty = True
    verify_set: set[int] = set()
    plan_sa = 0
    plan_dmax_a = 0
    plan_has_a = False
    runs_since_plan = 0
    cpu_at_plan = 0.0
    run_n = 0
    run_fail = 0

    def _add_remaining(t: int) -> None:
        nonlocal sum_rem, plan_dirty
        if t in remaining or t in confirmed_fail:
            return
        remaining.add(t)
        sum_rem += dur_by_num[t]
        est_cpu[t] = _est(t)
        est_cls[t] = _est_class(t)
        plan_dirty = True

    def _remove_remaining(t: int) -> None:
        nonlocal sum_rem
        if t not in remaining:
            return
        remaining.remove(t)
        sum_rem -= dur_by_num[t]
        verify_set.discard(t)

    def _add_confirmed(t: int) -> None:
        nonlocal sum_f, max_d_f, c_fail, plan_dirty
        if t in confirmed_fail:
            return
        confirmed_fail.add(t)
        d = dur_by_num[t]
        sum_f += d
        if d > max_d_f:
            max_d_f = d
        c_fail = -1  # mark dirty
        plan_dirty = True

    def _absorb_zero_ms() -> None:
        nonlocal zero_ms_absorbed
        if zero_ms_absorbed or not confirmed_fail:
            return
        zero_ms_absorbed = True
        zeros = [t for t in remaining if dur_by_num[t] == 0]
        for z in zeros:
            _remove_remaining(z)
            confirmed_fail.add(z)

    def _plog(*a: Any) -> None:
        if _PROF:
            print(f"[d] {commit_plan.commit}", *a, file=sys.stderr)

    # Fail-first home-test probe set: when confirmed_fail is empty on a wide commit, run the
    # highest-failure-probability home tests first (e.g. test_<dialect>.py for <dialect>.py,
    # test_optimizer.py::test_<pass> for optimizer/<pass>.py) so failing commits confirm a failure
    # in ~5-20ms and immediately unlock free slack absorption instead of running 300 unrelated tests!
    _home_files: set[str] = set()
    _home_opt_stems: set[str] = set()
    for _rel in set(commit_plan.patched_files) | set(commit_plan.deleted_files):
        if _rel.startswith("tests/") and _rel.endswith(".py"):
            _home_files.add(_rel)
        elif _rel.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")):
            _st = Path(_rel).stem
            if _st not in ("__init__", "dialect", "base"):
                _home_files.add(f"tests/dialects/test_{_st}.py")
                _DIALECT_CHILDREN = {
                    "hive": ("spark2", "spark", "databricks"),
                    "spark2": ("spark", "databricks"),
                    "spark": ("databricks",),
                    "postgres": ("redshift",),
                    "mysql": ("starrocks", "doris", "singlestore"),
                    "presto": ("trino", "athena"),
                    "trino": ("athena",),
                }
                for _ch in _DIALECT_CHILDREN.get(_st, ()):
                    _home_files.add(f"tests/dialects/test_{_ch}.py")
            elif _st == "dialect":
                _home_files.add("tests/dialects/test_dialect.py")
        elif _rel.startswith("sqlglot/optimizer/"):
            _st = Path(_rel).stem
            if _st != "__init__":
                _home_opt_stems.add(_st)
        elif _rel.startswith("sqlglot/executor/") or _rel == "sqlglot/planner.py":
            _home_files.add("tests/test_executor.py")
        elif _rel.startswith("sqlglot/expressions/"):
            _home_files.update(("tests/test_expressions.py", "tests/test_parser.py", "tests/test_generator.py"))
        elif _rel.startswith("sqlglot/") and _rel.endswith(".py"):
            _st = Path(_rel).stem
            if _st != "__init__":
                _home_files.add(f"tests/test_{_st}.py")
                if _st == "transforms":
                    _home_files.update(("tests/test_transpile.py", "tests/test_optimizer.py"))

    _home_cands = [
        t for t in remaining
        if est_cpu[t] <= 350 and dur_by_num[t] < 1500 and (
            num_to_test[t]["file"] in _home_files
            or (
                _home_opt_stems
                and num_to_test[t]["file"] == "tests/test_optimizer.py"
                and any(st in num_to_test[t]["id"].rsplit("::", 1)[-1] for st in _home_opt_stems)
            )
        )
    ]
    _home_cands.sort(key=lambda t: (-dur_by_num[t], est_cpu[t]))
    home_probe_set: set[int] = set(_home_cands[:8])

    def _priority(t: int) -> tuple[int, int, int]:
        e = est_cpu[t]
        r = dur_by_num[t]
        if num_to_test[t]["id"].endswith("::test_lazy_load"):
            return (-3, 0, e)
        if not confirmed_fail and t in home_probe_set:
            return (-2, -r, e)
        if e <= 800:
            bucket = 0
        elif r >= 3 * e:
            bucket = 1
        else:
            bucket = 2
        return (bucket, -r, e)

    def _make_plan() -> None:
        """Choose V (verify_set): heavy tests by enumeration, the rest as the best prefix by running price."""
        nonlocal verify_set, plan_sa, plan_dmax_a, plan_has_a, plan_dirty, runs_since_plan, cpu_at_plan
        plan_dirty = False
        runs_since_plan = 0
        cpu_at_plan = time.process_time()
        has_f = bool(confirmed_fail)
        p_hat = min(0.9, (run_fail + 0.3) / (run_n + 6.0))
        # The longest tests run first; every one that passes makes a failing commit less likely.
        exp_fail = _EXP_FAIL_MS * max(0.25, 1.0 - run_n / 16.0)
        rho = (_rho(0), _rho(1), _rho(2), _rho(3))
        heavy = sorted((t for t in remaining if dur_by_num[t] >= 1500), key=lambda t: -dur_by_num[t])[:5]
        hset = set(heavy)
        lights: list[tuple[float, int, int, float]] = []
        for t in remaining:
            if t in hset:
                continue
            d = dur_by_num[t]
            ec = est_cpu[t]
            c = est_cls[t]
            e = ec * rho[c if (c == 2 or ec > 20) else 3]
            lights.append((e / d if d > 0 else 1e18, t, d, e))
        lights.sort()
        n_l = len(lights)
        pre_e = [0.0] * (n_l + 1)
        pre_s = [0] * (n_l + 1)
        for i, (_, _, d, e) in enumerate(lights):
            pre_e[i + 1] = pre_e[i] + e
            pre_s[i + 1] = pre_s[i] + d
        suf_max = [0] * (n_l + 1)
        for i in range(n_l - 1, -1, -1):
            d = lights[i][2]
            suf_max[i] = d if d > suf_max[i + 1] else suf_max[i + 1]
        tot_l = pre_s[n_l]
        bps = range(n_l + 1) if n_l <= 48 else sorted({(n_l * j) // 48 for j in range(49)})
        n_h = len(heavy)
        # The TPC tests project their own cost after a ~1 s probe, so plan them at probe price.
        h_e = [
            min(_e_cal(h), _PROBE_MS) if num_to_test[h]["id"].endswith(_PROJ) else _e_cal(h)
            for h in heavy
        ]
        h_d = [dur_by_num[h] for h in heavy]
        full = (1 << n_h) - 1
        best_j = -1.0
        best_mask = 0
        best_k = 0
        for mask in range(1 << n_h):  # bit i set -> run heavy[i]
            he = 0.0
            hsa = 0
            hsv = 0
            hmax = 0
            for i in range(n_h):
                if (mask >> i) & 1:
                    he += h_e[i]
                    hsv += h_d[i]
                else:
                    hsa += h_d[i]
                    if h_d[i] > hmax:
                        hmax = h_d[i]
            for k in bps:
                e_tot = he + pre_e[k]
                if has_f or k < n_l or mask != full:
                    dm = max_d_f
                    if hmax > dm:
                        dm = hmax
                    if suf_max[k] > dm:
                        dm = suf_max[k]
                    j = e_tot + lb_cost_ms(sum_f + hsa + tot_l - pre_s[k] + p_hat * (hsv + pre_s[k]), dm)
                else:
                    j = e_tot + exp_fail
                if best_j < 0.0 or j < best_j - 1e-6:
                    best_j = j
                    best_mask = mask
                    best_k = k
        verify_set = {lights[i][1] for i in range(best_k)}
        plan_sa = tot_l - pre_s[best_k]
        plan_dmax_a = suf_max[best_k]
        plan_has_a = best_k < n_l or best_mask != full
        for i in range(n_h):
            if (best_mask >> i) & 1:
                verify_set.add(heavy[i])
            else:
                plan_sa += h_d[i]
                if h_d[i] > plan_dmax_a:
                    plan_dmax_a = h_d[i]
        if _PROF:
            _plog(
                f"plan rem={len(remaining)} verify={len(verify_set)} nf={len(confirmed_fail)} J={best_j:.0f} "
                f"rho={rho[0]:.2f}/{rho[1]:.2f}/{rho[3]:.2f} p={p_hat:.2f} sa={plan_sa}"
            )

    while remaining:
        if confirmed_fail:
            if not zero_ms_absorbed:
                _absorb_zero_ms()
                if not remaining:
                    break
            if c_fail < 0:
                c_fail = fast_cost_ms(confirmed_fail, dur_by_num)
                max_total_cap = max_total_for_cost_ms(c_fail, max_d_f)

            # SLACK-ABSORPTION THEOREM: only compute c_all when total duration CAN POSSIBLY fit in c_fail!
            if sum_f + sum_rem <= max_total_cap:
                c_all = fast_cost_ms(confirmed_fail | remaining, dur_by_num)
                if c_all == c_fail:
                    if len(confirmed_fail) + len(remaining) <= 100:
                        if optimal_cost_ms(confirmed_fail | remaining, dur_by_num) == optimal_cost_ms(confirmed_fail, dur_by_num):
                            _plog("absorb_slack", len(remaining))
                            confirmed_fail.update(remaining)
                            break
                    else:
                        _plog("absorb_slack", len(remaining))
                        confirmed_fail.update(remaining)
                        break

        # Two timed-out tests mean the patch hangs: every further hang burns seconds of CPU to
        # "save" a test that is failing anyway, so report the rest unrun.
        if timeout_count >= 2:
            _probe_lazy_import_crashes()
            _plog("absorb_timeouts", len(remaining))
            confirmed_fail.update(remaining)
            break

        if (
            plan_dirty
            or not verify_set
            or runs_since_plan >= 24
            or time.process_time() - cpu_at_plan > 0.5
        ):
            _make_plan()
            if not verify_set:
                _probe_lazy_import_crashes()
                _plog("absorb_plan", len(remaining))
                confirmed_fail.update(remaining)
                break

        t_next = min(verify_set, key=_priority)
        # Resolve a lazily deferred subtest filter NOW so the estimate reflects what will really run.
        if (
            engine is not None
            and t_next in commit_plan.deferred_subtests
            and not _helpers_p
            and num_to_test[t_next]["file"] not in commit_plan.patched_files
        ):
            resolved_filters[t_next] = engine.resolve_subtest_filter(commit_plan, t_next)
            _old_est = est_cpu[t_next]
            est_cpu[t_next] = _est(t_next)
            est_cls[t_next] = _est_class(t_next)
            if est_cpu[t_next] > 2 * _old_est + 20:
                plan_dirty = True  # much dearer than assumed: let the planner reconsider it
                continue
        d_t = dur_by_num[t_next]
        e_t = _e_cal(t_next)
        is_proj = num_to_test[t_next]["id"].endswith(_PROJ)
        # What passing can save, given the current plan: marginal cost of t_next on top of F | A.
        if confirmed_fail or plan_has_a:
            dm_base = max_d_f if max_d_f > plan_dmax_a else plan_dmax_a
            s_t = lb_cost_ms(sum_f + plan_sa + d_t, dm_base if dm_base > d_t else d_t) - lb_cost_ms(sum_f + plan_sa, dm_base)
        else:
            s_t = d_t + 1000.0

        # Running cannot pay for itself: report it unrun (dominates running whatever the outcome).
        if e_t >= 20.0 and (min(e_t, _PROBE_MS) if is_proj else e_t) >= s_t:
            _plog("absorb_test", t_next, f"{e_t:.0f}", f"{s_t:.0f}")
            _remove_remaining(t_next)
            _add_confirmed(t_next)
            continue

        # CPU limits for tests big enough to matter: never spend more than passing can save, and stop
        # early when a test runs far beyond its (calibrated) estimate.
        t_budget_sec: float | None = None
        t_limit_sec: float | None = None
        if d_t >= 300 or e_t >= 300.0:
            if is_proj:
                t_budget_sec = max(0.5, 0.90 * s_t / 1000.0)  # the per-query projection decides
                t_limit_sec = max(2.0, 2.0 * s_t / 1000.0)  # backstop only
            else:
                # Never above what passing can save; the floor absorbs one-off lazy imports (pandas/duckdb).
                floor_ms = 400.0 if "pandas" in sys.modules else 1900.0
                t_limit_sec = max(0.15, min(s_t, max(4.0 * e_t, 0.85 * s_t, floor_ms)) / 1000.0)

        _remove_remaining(t_next)
        _pt0 = time.process_time()
        passed = _run_one(t_next, cpu_budget_sec=t_budget_sec, cpu_limit_sec=t_limit_sec)
        _act_ms = (time.process_time() - _pt0) * 1000.0
        run_n += 1
        runs_since_plan += 1
        if passed:
            _c = _cal_idx(t_next)
            _est_raw = est_cpu[t_next]
            cal_act[_c] += _act_ms if _act_ms < 4.0 * _est_raw + 20.0 else 4.0 * _est_raw + 20.0
            cal_est[_c] += _est_raw
        elif not last_econ_aborted:
            run_fail += 1
        if _PROF:
            print(
                f"[t] {commit_plan.commit} {t_next} {num_to_test[t_next]['id'].split('::')[-1]} est={est_cpu[t_next]} "
                f"rec={dur_by_num[t_next]} cpu={_act_ms:.1f} "
                f"{'P' if passed else ('A' if last_econ_aborted else ('T' if last_timed_out else 'F'))} "
                f"budget={t_limit_sec} rem={len(remaining)} nf={len(confirmed_fail)} save={s_t:.0f}",
                file=sys.stderr,
            )
        if not passed:
            _add_confirmed(t_next)
            _probe_lazy_import_crashes()
            # If the test's file failed to import or setUpClass crashed, immediately mark ALL siblings as failing!
            tf = num_to_test[t_next]["file"]
            ck_str = "::".join(num_to_test[t_next]["id"].split("::")[:2])
            if tf in failed_test_files:
                # Pytest fails collection for EVERY test in an unimportable test file (even tests not in candidates!)
                broken_files = {tf} | set(maps.get("import_file_to_test_files", {}).get(tf, ()))
                for bf in broken_files:
                    failed_test_files.add(bf)
                    for x in file_to_all_tnums.get(bf, ()):
                        if x in remaining:
                            _remove_remaining(x)
                        _add_confirmed(x)
                # Runtime Import-Crash Escalation: if tf itself was NOT patched, the import crash came from
                # patched sqlglot/ code -> escalate to full broken-import closure so no other unimportable file is missed!
                if not import_escalated and tf not in commit_plan.patched_files and engine is not None:
                    import_escalated = True
                    extra: set[int] = set()
                    for rel in list(commit_plan.patched_files) + list(commit_plan.deleted_files):
                        if rel.endswith(".py"):
                            extra.update(engine._tests_for_broken_import(rel))
                    for x in extra:
                        if x not in confirmed_fail and x not in remaining:
                            if zero_ms_absorbed and dur_by_num[x] == 0:
                                _add_confirmed(x)
                            else:
                                _add_remaining(x)
            elif setup_classes_done.get(ck_str) is False:
                # If setUpClass crashed, EVERY test in that class fails (even if not originally in candidates!)
                for x in file_to_all_tnums.get(tf, ()):
                    if "::".join(num_to_test[x]["id"].split("::")[:2]) == ck_str:
                        if x in remaining:
                            _remove_remaining(x)
                        _add_confirmed(x)

    if _PROF:
        print(
            f"[done] {commit_plan.commit} selected={len(confirmed_fail)} cost_ms={fast_cost_ms(confirmed_fail, dur_by_num) if confirmed_fail else 0} "
            f"cpu_total={time.process_time():.3f}",
            file=sys.stderr,
        )
    return sorted(confirmed_fail)


# ---------------------------------------------------------------------------
# Dual-Fork Orchestrator (Pre-SQLGlot Zygote + Warm-SQLGlot Zygote)
# ---------------------------------------------------------------------------

# Wall-clock safety net so a batch can NEVER be rejected for exceeding the run timeout.
DEADLINE: float = float("inf")  # time.monotonic() value; set by selector.main()
PER_COMMIT_TIMEOUT: float = float(os.environ.get("BLAST_COMMIT_TIMEOUT", "90"))


class _ForkTimeout(Exception):
    pass


_PROF = bool(os.environ.get("BLAST_PROF"))


def _time_left() -> float:
    return DEADLINE - time.monotonic()


def _run_in_fork(fn, *args, timeout: float | None = None) -> Any:
    """Fork a clean CoW child process, redirect stdout->stderr in child, return pickled result.
    Kills child after `timeout` seconds (default: per-commit cap, never past DEADLINE)."""
    import select as _select

    budget = _time_left()
    if timeout is None:
        timeout = PER_COMMIT_TIMEOUT
    timeout = min(timeout, budget)
    if timeout <= 0.5:
        raise _ForkTimeout("global deadline reached")
    started = time.monotonic()
    r_fd, w_fd = os.pipe()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(r_fd)
        # Ensure child can NEVER write to parent's stdout (fd 1 -> fd 2)!
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        try:
            result = fn(*args)
            data = pickle.dumps(("ok", result), protocol=pickle.HIGHEST_PROTOCOL)
        except BaseException as exc:
            if _PROF:
                import traceback
                traceback.print_exc()
            data = pickle.dumps(("err", repr(exc)), protocol=pickle.HIGHEST_PROTOCOL)
        try:
            view = memoryview(data)
            written = 0
            while written < len(data):
                written += os.write(w_fd, view[written:])
        finally:
            os.close(w_fd)
            os._exit(0)
    else:
        os.close(w_fd)
        chunks: list[bytes] = []
        while True:
            left = timeout - (time.monotonic() - started)
            if math.isinf(left):
                ready = [r_fd]
            else:
                ready = _select.select([r_fd], [], [], max(0.0, left))[0] if left > 0 else []
            if not ready:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
                os.close(r_fd)
                os.waitpid(pid, 0)
                raise _ForkTimeout(f"child exceeded {timeout:.1f}s")
            chunk = os.read(r_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(r_fd)
        _, _, _ru = os.wait4(pid, 0)
        if _PROF:
            _lbl = getattr(args[0], "commit", fn.__name__) if args else fn.__name__
            _grp = getattr(args[0], "group", "") if args else ""
            _nc = len(getattr(args[0], "candidates", ())) if args else 0
            print(
                f"[prof] {_lbl} group={_grp} fn={fn.__name__} cands={_nc} "
                f"cpu={_ru.ru_utime + _ru.ru_stime:.3f} wall={time.monotonic() - started:.3f}",
                file=sys.stderr,
            )
        status, payload = pickle.loads(b"".join(chunks))
        if status == "ok":
            return payload
        raise RuntimeError(f"Child verification failed: {payload}")


def verify_batch(
    plans: list[CommitPlan],
    maps: dict[str, Any],
    num_to_test: dict[int, dict],
    dur_by_num: dict[int, int],
    sqlglot_root: Path,
    engine: Any = None,
) -> dict[str, list[int]]:
    """Verify all non-noop commits using Dual Warm Zygotes (C-accelerated /sqlglot-c + Pure /sqlglot)."""
    results: dict[str, list[int]] = {}
    active_plans: list[CommitPlan] = []
    for p in plans:
        if p.is_noop:
            results[p.commit] = []
        else:
            active_plans.append(p)

    if not active_plans:
        return results

    # NOTE: duckdb, numpy, pandas are imported lazily ONLY when a test actually executes them!
    os.environ["SKIP_INTEGRATION"] = "0"
    compiled_c_files = _get_compiled_c_files(_SQLGLOT_C_ROOT)
    has_sqlglot_c = bool(compiled_c_files) and _SQLGLOT_C_ROOT.is_dir()
    compiled_cb_files = _get_compiled_c_files(_SQLGLOT_CB_ROOT)
    has_sqlglot_cb = bool(compiled_cb_files) and _SQLGLOT_CB_ROOT.is_dir()

    lazy_tid = next(
        (n for n, t in num_to_test.items() if t["id"].endswith("::test_lazy_load")),
        None,
    )
    pure_only_tids: set[int] = {
        n for n, t in num_to_test.items()
        if t["file"] == "tests/test_sqlglotc.py" or t["id"] in _PURE_ONLY_TEST_IDS
    }

    def _can_use_c(plan: CommitPlan) -> bool:
        if not has_sqlglot_c:
            return False
        touched = set(plan.patched_files) | set(plan.deleted_files)
        return touched.isdisjoint(compiled_c_files)

    def _can_use_cb(plan: CommitPlan) -> bool:
        if not has_sqlglot_cb:
            return False
        touched = set(plan.patched_files) | set(plan.deleted_files)
        return touched.isdisjoint(compiled_cb_files)

    # Helper to run a full_fork commit in a clean child BEFORE sqlglot is imported in parent!
    def _run_full_fork_child(plan: CommitPlan, root_override: Path | None = None) -> list[int]:
        if root_override is not None:
            use_root = root_override
        elif _can_use_c(plan):
            use_root = _SQLGLOT_C_ROOT
        elif _can_use_cb(plan):
            use_root = _SQLGLOT_CB_ROOT
        else:
            use_root = sqlglot_root
        # Ensure use_root is first on sys.path (never mix /sqlglot, /sqlglot-c, /sqlglot-c-base!)
        for rp in (str(sqlglot_root), str(_SQLGLOT_C_ROOT), str(_SQLGLOT_CB_ROOT)):
            while rp in sys.path:
                sys.path.remove(rp)
        sys.path.insert(0, str(use_root))
        ov_dir: Path | None = None
        if lazy_tid is not None and lazy_tid in plan.candidates and any(
            rel.startswith("sqlglot/") for rel in list(plan.patched_files) + list(plan.deleted_files)
        ):
            # Always point test_lazy_load's subprocess overlay at pure /sqlglot (never mix .so into overlay!)
            ov_dir = _create_disk_overlay_for_subprocess(plan.patched_files, sqlglot_root, plan.deleted_files)
            os.environ["PYTHONPATH"] = f"{ov_dir}:{sqlglot_root}"

        finder = _InMemoryFinder(plan.patched_files, use_root, plan.deleted_files)
        sys.meta_path.insert(0, finder)
        restore_open = _install_open_overlay(plan.patched_files, use_root, plan.deleted_files)
        try:
            return _execute_commit_verification(
                plan, maps, num_to_test, dur_by_num, use_root, {}, set(plan.syntax_error_files), engine=engine, ov_dir=ov_dir
            )
        finally:
            restore_open()
            if ov_dir is not None:
                import shutil
                shutil.rmtree(ov_dir, ignore_errors=True)

    def _is_lazy_only(plan: CommitPlan) -> bool:
        if plan.deleted_files or plan.syntax_error_files:
            return False
        for rel in plan.patched_files:
            if not rel.endswith(".py"):
                continue
            if rel.startswith((
                "sqlglot/dialects/",
                "sqlglot/parsers/",
                "sqlglot/generators/",
                "sqlglot/typing/",
                "sqlglot/optimizer/",
                "sqlglot/executor/",
                "tests/",
                "sqlglotc/",
            )):
                if rel in (
                    "sqlglot/dialects/__init__.py",
                    "sqlglot/dialects/dialect.py",
                    "sqlglot/parsers/__init__.py",
                    "sqlglot/parsers/base.py",
                    "sqlglot/generators/__init__.py",
                    "sqlglot/typing/__init__.py",
                    "sqlglot/optimizer/__init__.py",
                    "sqlglot/optimizer/scope.py",
                    "tests/helpers.py",
                ):
                    return False
            elif rel in ("sqlglot/diff.py", "sqlglot/lineage.py", "sqlglot/anonymize.py", "sqlglot/planner.py", "sqlglot/serde.py"):
                continue
            else:
                return False
        return True

    def _split_plans(group_plans: list[CommitPlan]) -> tuple[list[CommitPlan], list[CommitPlan], list[CommitPlan], list[CommitPlan]]:
        eager_f: list[CommitPlan] = []
        lazy_f: list[CommitPlan] = []
        hot_f: list[CommitPlan] = []
        fix_f: list[CommitPlan] = []
        for p in group_plans:
            if p.group == "fixture":
                fix_f.append(p)
            elif p.group == "hot_func":
                if _is_lazy_only(p):
                    lazy_f.append(p)
                else:
                    hot_f.append(p)
            else:
                if _is_lazy_only(p):
                    lazy_f.append(p)
                else:
                    eager_f.append(p)
        return eager_f, lazy_f, hot_f, fix_f

    c_plans_all: list[CommitPlan] = []
    cb_plans_all: list[CommitPlan] = []
    py_plans_all: list[CommitPlan] = []
    pure_hold_by_commit: dict[str, tuple[CommitPlan, set[int]]] = {}

    for p in active_plans:
        target_bucket: list[CommitPlan] | None = None
        if _can_use_c(p):
            target_bucket = c_plans_all
        elif _can_use_cb(p):
            target_bucket = cb_plans_all
        if target_bucket is not None:
            ph = p.candidates & pure_only_tids
            if ph and p.candidates <= pure_only_tids:
                py_plans_all.append(p)
            elif ph:
                c_p = copy.copy(p)
                c_p.candidates = p.candidates - ph
                c_p.force_report = p.force_report - ph
                pure_hold_by_commit[p.commit] = (p, ph)
                target_bucket.append(c_p)
            else:
                target_bucket.append(p)
        else:
            py_plans_all.append(p)

    c_eager, c_lazy, c_hot, c_fix = _split_plans(c_plans_all)
    cb_eager, cb_lazy, cb_hot, cb_fix = _split_plans(cb_plans_all)

    # Preload pure-Python fixture cache (zero sqlglot imports!) and shared subtest maps ONCE before os.fork()!
    _load_fixture_cache()
    if engine is not None and len(plans) >= 3:
        st_freq_all: dict[int, int] = {}
        for p in plans:
            for tnum in p.deferred_subtests:
                st_freq_all[tnum] = st_freq_all.get(tnum, 0) + 1
        for tnum, cnt in st_freq_all.items():
            if cnt >= 2:
                engine._get_subtests(tnum)

    # Freeze parent heap objects before os.fork() to prevent Linux CoW page faults & GC scans in children!
    gc.freeze()

    def _safe_fallback_tests(plan: CommitPlan) -> list[int]:
        out = set(plan.candidates) | set(plan.force_report)
        if engine is not None:
            for rel in list(plan.patched_files) + list(plan.deleted_files):
                if rel.endswith(".py"):
                    out.update(engine._tests_for_broken_import(rel))
        return sorted(out)

    def _run_warm_zygote(
        warm_root: Path,
        wz_lazy: list[CommitPlan],
        wz_hot: list[CommitPlan],
        wz_fix: list[CommitPlan],
    ) -> tuple[dict[str, list[int]], list[str]]:
        """Runs inside ONE forked child that imports sqlglot core from `warm_root` ONCE:
        Phase A: runs wz_lazy in 1ms CoW forks with zero-dialect & deferred-dialect shared test modules preloaded!
        Phase B: pre-imports remaining shared test modules, freezes heap, then runs wz_hot FIRST and wz_fix AFTER."""
        for rp in (str(sqlglot_root), str(_SQLGLOT_C_ROOT), str(_SQLGLOT_CB_ROOT)):
            while rp in sys.path:
                sys.path.remove(rp)
        sys.path.insert(0, str(warm_root))
        import sqlglot  # noqa: F401
        import tests.helpers  # noqa: F401
        _measure_speed_scale()

        warm_results: dict[str, list[int]] = {}
        fallback_commits: list[str] = []

        def _snapshot_loaded() -> set[str]:
            lf: set[str] = set()
            root_prefix = str(warm_root.resolve()) + "/"
            for m in list(sys.modules.values()):
                mf = getattr(m, "__file__", None)
                if mf and mf.startswith(root_prefix):
                    rel_m = mf[len(root_prefix):]
                    if ".cpython-" in rel_m and rel_m.endswith(".so"):
                        rel_m = rel_m.split(".cpython-", 1)[0] + ".py"
                    lf.add(rel_m)
            return lf

        all_warm_plans = wz_lazy + wz_fix + wz_hot
        if engine is not None and len(all_warm_plans) >= 3:
            st_freq: dict[int, int] = {}
            for p in all_warm_plans:
                for tnum in p.deferred_subtests:
                    st_freq[tnum] = st_freq.get(tnum, 0) + 1
            for tnum, cnt in st_freq.items():
                if cnt >= 3:
                    engine._get_subtests(tnum)

        all_patched_py = {
            rel for p in all_warm_plans for rel in p.patched_files if rel.endswith(".py")
        }
        loaded_files = _snapshot_loaded()
        opt_conflict_plans: list[CommitPlan] = []
        dialect_conflict_plans: list[CommitPlan] = []
        dialect_safe_plans: list[CommitPlan] = []
        _DIALECT_PRELOAD_CONFLICTS = frozenset({
            "sqlglot/dialects/duckdb.py", "sqlglot/parsers/duckdb.py", "sqlglot/generators/duckdb.py",
            "sqlglot/dialects/bigquery.py", "sqlglot/parsers/bigquery.py", "sqlglot/generators/bigquery.py",
            "sqlglot/dialects/hive.py", "sqlglot/parsers/hive.py", "sqlglot/generators/hive.py",
            "sqlglot/dialects/snowflake.py", "sqlglot/parsers/snowflake.py", "sqlglot/generators/snowflake.py",
            "sqlglot/dialects/spark2.py", "sqlglot/parsers/spark2.py", "sqlglot/generators/spark2.py",
            "sqlglot/dialects/mysql.py", "sqlglot/parsers/mysql.py", "sqlglot/generators/mysql.py",
            "tests/dialects/test_dialect.py", "tests/dialects/test_mysql.py",
        })
        for plan in wz_lazy:
            py_rels = [rel for rel in plan.patched_files if rel.endswith(".py")]
            if any(
                rel in ("tests/test_optimizer.py", "tests/test_schema.py") or rel.startswith("sqlglot/optimizer/")
                for rel in py_rels
            ):
                opt_conflict_plans.append(plan)
            elif any(rel in _DIALECT_PRELOAD_CONFLICTS or rel.startswith("tests/dialects/") for rel in py_rels):
                dialect_conflict_plans.append(plan)
            else:
                dialect_safe_plans.append(plan)

        _ZERO_DIALECT_TESTS = (
            "tests/test_parser.py",
            "tests/test_generator.py",
            "tests/test_serde.py",
            "tests/test_transpile.py",
            "tests/test_diff.py",
        )
        for ztf in _ZERO_DIALECT_TESTS:
            if ztf in all_patched_py:
                continue
            if ztf == "tests/test_diff.py" and "sqlglot/diff.py" in all_patched_py:
                continue
            if ztf == "tests/test_serde.py" and "sqlglot/serde.py" in all_patched_py:
                continue
            if ztf == "tests/test_generator.py" and any(
                r in all_patched_py for r in ("sqlglot/parsers/snowflake.py", "sqlglot/dialects/snowflake.py")
            ):
                continue
            z_users = sum(
                1 for p in all_warm_plans
                if any(num_to_test[t]["file"] == ztf for t in p.candidates)
            )
            if z_users >= 2:
                try:
                    _import_test_module_lazy(ztf.removesuffix(".py").replace("/", "."), False)
                except Exception:
                    pass
        loaded_files = _snapshot_loaded()

        if opt_conflict_plans:
            gc.freeze()
        for plan in opt_conflict_plans:
            if any(rel in loaded_files for rel in plan.patched_files if rel.endswith(".py")):
                fallback_commits.append(plan.commit)
                continue
            try:
                warm_results[plan.commit] = _run_in_fork(_run_full_fork_child, plan, warm_root)
            except Exception:
                fallback_commits.append(plan.commit)

        rem_after_opt = dialect_conflict_plans + dialect_safe_plans + wz_fix + wz_hot
        if "tests/test_optimizer.py" not in all_patched_py:
            opt_users = sum(
                1 for p in rem_after_opt
                if any(num_to_test[t]["file"] == "tests/test_optimizer.py" for t in p.candidates)
            )
            if opt_users >= 2:
                try:
                    _import_test_module_lazy("tests.test_optimizer", False)
                except Exception:
                    pass

        if rem_after_opt and "tests/dialects/test_dialect.py" not in all_patched_py:
            td_file_freq: dict[str, int] = {}
            td_dial_users = 0
            for p in rem_after_opt:
                seen_tf = {num_to_test[t]["file"] for t in p.candidates}
                if any(tf.startswith("tests/dialects/") for tf in seen_tf):
                    td_dial_users += 1
                for tf in seen_tf:
                    if tf.startswith("tests/dialects/"):
                        td_file_freq[tf] = td_file_freq.get(tf, 0) + 1
            if td_dial_users >= 2 and _preload_test_dialect_deferred(warm_root):
                mysql_patched = any(r.endswith("/mysql.py") or r == "tests/dialects/test_mysql.py" for r in all_patched_py)
                ch_patched = any(r.endswith("/clickhouse.py") or r == "tests/dialects/test_clickhouse.py" for r in all_patched_py)
                for tf, cnt in td_file_freq.items():
                    if cnt < 2 or tf in all_patched_py or tf == "tests/dialects/test_dialect.py":
                        continue
                    if tf == "tests/dialects/test_mysql.py" and mysql_patched:
                        continue
                    if tf == "tests/dialects/test_clickhouse.py" and ch_patched:
                        continue
                    try:
                        _import_test_module_lazy(tf.removesuffix(".py").replace("/", "."), False)
                    except Exception:
                        pass
        loaded_files = _snapshot_loaded()

        if dialect_conflict_plans:
            gc.freeze()
        for plan in dialect_conflict_plans:
            if any(rel in loaded_files for rel in plan.patched_files if rel.endswith(".py")):
                fallback_commits.append(plan.commit)
                continue
            try:
                warm_results[plan.commit] = _run_in_fork(_run_full_fork_child, plan, warm_root)
            except Exception:
                fallback_commits.append(plan.commit)

        if dialect_safe_plans:
            gc.freeze()
        for plan in dialect_safe_plans:
            if any(rel in loaded_files for rel in plan.patched_files if rel.endswith(".py")):
                fallback_commits.append(plan.commit)
                continue
            try:
                warm_results[plan.commit] = _run_in_fork(_run_full_fork_child, plan, warm_root)
            except Exception:
                fallback_commits.append(plan.commit)

        if not (wz_fix or wz_hot):
            return warm_results, fallback_commits

        file_freq: dict[str, int] = {}
        for p in (wz_fix + wz_hot):
            seen_tf = {num_to_test[t]["file"] for t in p.candidates}
            for tf in seen_tf:
                if tf in ("tests/dialects/test_mysql.py", "tests/dialects/test_clickhouse.py", "tests/dialects/test_dialect.py"):
                    continue
                file_freq[tf] = file_freq.get(tf, 0) + 1
        for tf, cnt in file_freq.items():
            if cnt >= 2 and tf not in all_patched_py:
                try:
                    mod_name = tf.removesuffix(".py").replace("/", ".")
                    if mod_name not in sys.modules:
                        _import_test_module_lazy(mod_name, False)
                except Exception:
                    pass

        mod_cnt: dict[str, int] = {}
        for p in wz_hot:
            for rel in p.py_analyses:
                if rel.startswith("sqlglot/") and not rel.endswith("/__init__.py"):
                    m = rel.removesuffix(".py").replace("/", ".")
                    mod_cnt[m] = mod_cnt.get(m, 0) + 1
        for m, cnt in mod_cnt.items():
            if cnt >= 2:
                try:
                    importlib.import_module(m)
                except Exception:
                    pass

        gc.freeze()

        def _run_hot_child(plan: CommitPlan) -> tuple[bool, list[int]]:
            ok = _apply_hot_func_patches(plan, warm_root)
            if not ok:
                return False, []
            restore_open = _install_open_overlay(plan.patched_files, warm_root)
            try:
                failing = _execute_commit_verification(
                    plan, maps, num_to_test, dur_by_num, warm_root, {}, set(), engine=engine
                )
                return True, failing
            finally:
                restore_open()

        for plan in wz_hot:
            try:
                ok, failing = _run_in_fork(_run_hot_child, plan)
                if ok:
                    warm_results[plan.commit] = failing
                else:
                    fallback_commits.append(plan.commit)
            except Exception:
                fallback_commits.append(plan.commit)

        def _run_fixture_child(plan: CommitPlan) -> list[int]:
            restore_open = _install_open_overlay(plan.patched_files, warm_root, plan.deleted_files)
            try:
                return _execute_commit_verification(
                    plan, maps, num_to_test, dur_by_num, warm_root, {}, set(), engine=engine
                )
            finally:
                restore_open()

        for plan in wz_fix:
            try:
                warm_results[plan.commit] = _run_in_fork(_run_fixture_child, plan)
            except Exception:
                fallback_commits.append(plan.commit)

        return warm_results, fallback_commits

    def _run_tier(
        tier_root: Path,
        t_eager: list[CommitPlan],
        t_lazy: list[CommitPlan],
        t_hot: list[CommitPlan],
        t_fix: list[CommitPlan],
    ) -> None:
        for plan in t_eager:
            try:
                results[plan.commit] = _run_in_fork(_run_full_fork_child, plan, tier_root)
            except Exception:
                results[plan.commit] = _safe_fallback_tests(plan)

        if not (t_lazy or t_hot or t_fix):
            return

        try:
            warm_results, fallback_commits = _run_in_fork(
                _run_warm_zygote, tier_root, t_lazy, t_hot, t_fix, timeout=float("inf")
            )
        except Exception:
            warm_results, fallback_commits = {}, []
            for plan in t_lazy + t_fix + t_hot:
                results[plan.commit] = _safe_fallback_tests(plan)
        results.update(warm_results)

        if fallback_commits:
            plan_by_id = {p.commit: p for p in (t_lazy + t_fix + t_hot)}
            for cid in fallback_commits:
                plan = plan_by_id[cid]
                try:
                    results[cid] = _run_in_fork(_run_full_fork_child, plan, tier_root)
                except Exception:
                    results[cid] = _safe_fallback_tests(plan)

    # Tier 1: C-accelerated /sqlglot-c (94 mods, pure-Python optimizer/*; eager forks + C-Warm Zygote)
    if has_sqlglot_c and c_plans_all:
        _run_tier(_SQLGLOT_C_ROOT, c_eager, c_lazy, c_hot, c_fix)

    # Tier 2: C-base /sqlglot-c-base (22 core mods; covers parsers/*, generators/*, parser.py, generator.py!)
    if has_sqlglot_cb and cb_plans_all:
        _run_tier(_SQLGLOT_CB_ROOT, cb_eager, cb_lazy, cb_hot, cb_fix)

    # Resolve tiny _PURE_ONLY_TIDS held aside from C/C-base runs: absorb for free if zero slack cost, else verify in Pure tier!
    pure_tail_map: dict[str, str] = {}
    for orig_cid, (orig_plan, ph) in pure_hold_by_commit.items():
        c_fail = set(results.get(orig_cid, []))
        if c_fail and fast_cost_ms(c_fail | ph, dur_by_num) == fast_cost_ms(c_fail, dur_by_num):
            results[orig_cid] = sorted(c_fail | ph)
        else:
            tail_cid = f"__pure_tail__{orig_cid}"
            tail_p = copy.copy(orig_plan)
            tail_p.commit = tail_cid
            tail_p.candidates = set(ph)
            tail_p.force_report = set(orig_plan.force_report & ph)
            pure_tail_map[tail_cid] = orig_cid
            py_plans_all.append(tail_p)

    # Tier 2: Pure-Python /sqlglot (compiled-file commits + pure-only tails; eager forks + Pure-Warm Zygote)
    if py_plans_all:
        py_eager, py_lazy, py_hot, py_fix = _split_plans(py_plans_all)
        _run_tier(sqlglot_root, py_eager, py_lazy, py_hot, py_fix)

    for tail_cid, orig_cid in pure_tail_map.items():
        t_fail = results.pop(tail_cid, [])
        if t_fail:
            results[orig_cid] = sorted(set(results.get(orig_cid, [])) | set(t_fail))

    for plan in plans:
        if plan.commit not in results:
            results[plan.commit] = _safe_fallback_tests(plan)

    return results
