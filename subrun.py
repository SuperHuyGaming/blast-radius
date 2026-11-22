"""Runs part of test_execute_tpcds against a patched tree, to drop it when it passes.

The test executes 99 TPC-DS queries, one pool task each, and costs 21 s. The
unit map (maps/units.bin, built from the pinned commit) says which queries run
which sqlglot lines. A query that runs no changed line behaves as it did, so
when the queries that do run one all pass under the patch, the test passes.

A run forks the selector. The child imports the patched tree from an overlay in
/tmp and runs the real test method, with its query list cut to the covering
queries and the process pool replaced by an in-process one. Anything but a
clean pass keeps the test selected.
"""

import functools
import json
import os
import pickle
import resource
import select
import shutil
import signal
import sys
import tempfile
import time
import zlib

TEST_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
METHOD = "test_execute_tpcds"
TPCDS_DIR = "tests/fixtures/optimizer/tpc-ds/"
TABLE_PY = "sqlglot/executor/table.py"
TPCH_CSV = "tests/fixtures/optimizer/tpc-h/"  # patched_files refuses TPCDS_DIR: these are the other CSVs setUpClass reads
ROWS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rows.pkl")  # save_rows, at image build
WORTH = 1.25  # run when the saving is this many times the CPU of a full pass: a failing run stops at its first failing query
SECONDS_PER_COMMIT = 4.0  # wall the batch may spend: the scorer allows 4.5 s a commit (270 s / ~60 in the final)
MAX_WALL = 150.0
# a run over this is killed: the container has 4 GB for everything. No RLIMIT_DATA: DuckDB
# reserves address space per thread (31 threads on a 128-core host), so any cap low enough
# to matter fails setUpClass with "Out of Memory Error" and every run reads F
MAX_RSS_MB = 1536
RSS_POLL_S = 0.25
STARTED = time.monotonic()  # the selector imports this module as it starts
BENCH_REF_S = 0.0072  # bench() on the machine units.bin was recorded on (Apple M-series, venv python)


@functools.cache
def bench():
    """CPU seconds of a fixed pure-Python loop, best of 3: how fast this machine
    runs the interpreter-bound queries, against BENCH_REF_S."""
    best = 9.0
    for _ in range(3):
        t = time.process_time()
        s = 0
        for i in range(300000):
            s += i * i
        best = min(best, time.process_time() - t)
    return best


def rss_mb(pid):
    """Resident memory of a process; 0 when it can't be read."""
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except (OSError, ValueError, IndexError):
        pass
    try:  # no /proc (macOS)
        import subprocess

        return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout or 0) / 1024
    except (OSError, ValueError):
        return 0


class LazyFuture:
    """Runs its task when the result is asked for, so a test that stops at the
    first failing result leaves the later tasks unrun."""

    def __init__(self, blob):
        self._blob = blob
        self._done = False
        self._value = None

    def result(self, timeout=None):
        if not self._done:
            fn, args, kwargs = pickle.loads(self._blob)
            self._value = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
            self._done = True
        return self._value


class SyncPool:
    """In-process ProcessPoolExecutor. Arguments and results still round-trip
    through pickle, as they do between the test and its spawned workers."""

    # the initargs are the fixture Tables: their round trip is a plain copy (~90 ms) unless
    # TABLE_PY, which alone defines how a Table pickles, changed
    copy_init = True

    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*(pickle.loads(pickle.dumps(initargs)) if self.copy_init else initargs))

    def submit(self, fn, *args, **kwargs):
        return LazyFuture(pickle.dumps((fn, args, kwargs)))

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class LazyDuckDB:
    """duckdb.connect() for setUpClass: a connection that opens, and creates the
    views queued before, on its first query. Only a query whose result `frames`
    lacks reaches one."""

    @classmethod
    def connect(cls):
        return cls()

    def __init__(self):
        self.views = []
        self.conn = None

    def execute(self, sql):
        if self.conn is None:
            if sql.lstrip().startswith("CREATE VIEW"):
                self.views.append(sql)
                return None
            import duckdb

            self.conn = duckdb.connect()
            for view in self.views:
                self.conn.execute(view)
        return self.conn.execute(sql)

    def close(self):
        if self.conn is not None:
            self.conn.close()


def setup_executor(te, rows, setup):
    """Runs `setup`, TestExecutor.setUpClass, over header-only CSVs with the DuckDB
    views queued until a query, then builds each table from `rows` (save_rows),
    the tuples it would have parsed: the same class state, ~0.9 s less a run."""
    open_file, duckdb = te.open_file, te.duckdb
    te.open_file = lambda name: iter(["\n"])
    te.duckdb = LazyDuckDB
    try:
        setup()
    finally:
        te.open_file, te.duckdb = open_file, duckdb
    for attr, schema in (("tpch_tables", te.TPCH_SCHEMA), ("tpcds_tables", te.TPCDS_SCHEMA)):
        for table, columns in schema.items():
            getattr(te.TestExecutor, attr)[table] = te.Table(columns=columns, rows=rows[attr][table])


def rows_ok(files):
    """True when the image has save_rows's rows and the patch leaves the CSVs they come from."""
    return os.path.exists(ROWS) and not any(p.startswith(TPCH_CSV) and not p.endswith(".sql") for p in files)


@functools.cache
def load_rows():
    with open(ROWS, "rb") as f:
        return pickle.load(f)


def run_units(root, keep, frames, n_units, hook=None, capture=None, rows=None, copy_init=True):
    """True when test_execute_tpcds, cut to the queries in `keep`, passes on the
    tree at `root`. `frames` maps a DuckDB query text to its result on the
    pinned fixtures; a text it lacks is run on DuckDB, as the test does. `rows`
    (save_rows) stands in for setUpClass's parse of the fixture CSVs."""
    import concurrent.futures as cf
    import unittest

    os.environ["SKIP_INTEGRATION"] = "0"
    sys.dont_write_bytecode = True
    sys.path.insert(0, root)
    cf.ProcessPoolExecutor = SyncPool
    SyncPool.copy_init = copy_init
    import tests.test_executor as te

    cls = te.TestExecutor
    if rows is None:
        cls.setUpClass()
    else:
        setup_executor(te, rows, cls.setUpClass)
    try:
        sqls = cls.tpcds_sqls
        if len(sqls) != n_units:
            return False
        cls.tpcds_sqls = [sqls[i] for i in keep]
        transpile = te.transpile

        def cached_execute(self, sql, tpch=True):
            if sql not in self.cache:
                query = transpile(sql, write="duckdb")[0]
                frame = frames.get(query)
                if frame is None:
                    conn = self.tpch_conn if tpch else self.tpcds_conn
                    frame = conn.execute(query).fetchdf()
                    if capture is not None:
                        capture[query] = frame
                self.cache[sql] = frame.copy()
            return self.cache[sql]

        cls.cached_execute = cached_execute
        if hook is not None:
            hook()
        result = unittest.TestResult()
        result.failfast = True
        cls(METHOD).run(result)
        return result.testsRun == 1 and result.wasSuccessful() and not result.skipped
    finally:
        cls.tearDownClass()


def save_rows(root, out):
    """Pickles the rows TestExecutor.setUpClass parses from the TPC-H and TPC-DS
    CSVs at `root` (the image build runs this: build CPU is not charged)."""
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, root)
    import tests.test_executor as te

    cls = te.TestExecutor
    cls.setUpClass()
    try:
        rows = {attr: {t: v.rows for t, v in getattr(cls, attr).items()} for attr in ("tpch_tables", "tpcds_tables")}
    finally:
        cls.tearDownClass()
    with open(out, "wb") as f:
        pickle.dump(rows, f, protocol=pickle.HIGHEST_PROTOCOL)


def patched_files(src, fps, apply_hunks):
    """{path: new text} for a patch a run can stand in for, else None: existing
    text files changed in place, none of them test code or TPC-DS data, every
    hunk matching the pinned source."""
    files = {}
    for fp in fps:
        path = fp.old_path
        if fp.binary or path is None or fp.new_path != path or path in files:
            return None
        if path.startswith(TPCDS_DIR) or (path.startswith("tests/") and path.endswith(".py")):
            return None
        # only library code and fixture data: config (pyproject.toml, setup.cfg, conftest) changes
        # how pytest runs the real test, which the unittest-driven cut never sees
        if not ((path.startswith("sqlglot/") and path.endswith(".py")) or path.startswith("tests/fixtures/")):
            return None
        if os.path.isabs(path) or ".." in path.split("/"):
            return None
        try:
            with open(os.path.join(src, path), encoding="utf-8", newline="") as f:
                old = f.read()
        except (OSError, UnicodeDecodeError):
            return None
        lines = old.split("\n")
        for start, length, _, _, body in fp.hunks:
            at = start - 1 if length > 0 else start
            for tag, text in body:
                if tag == "+":
                    continue
                if at >= len(lines) or lines[at] != text:
                    return None
                at += 1
        files[path] = apply_hunks(old, fp.hunks)[0]
    return files


def build_overlay(src, files, dst):
    """A tree at `dst` that reads as `src` with `files` replaced: symlinks to
    everything unchanged, real directories down to each replaced file."""
    real_dirs = {""}
    for path in files:
        parts = path.split("/")[:-1]
        for k in range(1, len(parts) + 1):
            real_dirs.add("/".join(parts[:k]))
    for rel in sorted(real_dirs):
        here = os.path.join(dst, rel)
        os.makedirs(here, exist_ok=True)
        there = os.path.join(src, rel)
        prefix = rel + "/" if rel else ""
        stale = {os.path.basename(p)[:-3] for p in files if p.startswith(prefix) and "/" not in p[len(prefix):] and p.endswith(".py")}
        for name in os.listdir(there):
            child = prefix + name
            if child in real_dirs:
                continue
            if child in files:
                with open(os.path.join(here, name), "w", encoding="utf-8", newline="") as f:
                    f.write(files[child])
            elif name == "__pycache__" and stale:
                # bytecode of the replaced modules must not be found
                os.mkdir(os.path.join(here, name))
                for pyc in os.listdir(os.path.join(there, name)):
                    if pyc.split(".", 1)[0] not in stale:
                        os.symlink(os.path.join(there, name, pyc), os.path.join(here, name, pyc))
            else:
                os.symlink(os.path.join(there, name), os.path.join(here, name))


def load_units(maps_dir):
    with open(os.path.join(maps_dir, "units.bin"), "rb") as f:
        return pickle.loads(zlib.decompress(f.read()))


def fork_run(src, files, body, cap, wall, done):
    """Runs body(tree, w) in a forked child over an overlay of `src` with `files`
    replaced, the child's stdio on /dev/null and its CPU capped at `cap` s. The
    parent reads what the child writes to `w` until done(bytes so far) is true,
    the child exits, `wall` s pass or its RSS passes MAX_RSS_MB, then kills the
    child's process group. Returns (bytes read, CPU of the child and its reaped children)."""
    tree = os.path.realpath(tempfile.mkdtemp(prefix="blast-"))
    pid = 0
    r = w = -1
    out = b""
    try:
        build_overlay(src, files, tree)
        r, w = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            _child(tree, body, cap, r, w)
        os.close(w)
        w = -1
        end = time.monotonic() + wall
        while not done(out):
            if select.select([r], [], [], RSS_POLL_S)[0]:
                chunk = os.read(r, 65536)
                if not chunk:
                    break
                out += chunk
                continue
            if time.monotonic() > end or rss_mb(pid) > MAX_RSS_MB:
                break
        return out, _reap(pid)
    except OSError:
        if pid > 0:
            return out, _reap(pid)
        return out, 0.0
    finally:
        for fd in (r, w):
            if fd >= 0:
                os.close(fd)
        shutil.rmtree(tree, ignore_errors=True)


def _reap(pid):
    """Kills the child's group (it may have left pool workers behind) and reaps it."""
    for kill in (os.killpg, os.kill):
        try:
            kill(pid, signal.SIGKILL)
        except OSError:
            pass
    usage = os.wait4(pid, 0)[2]
    return usage.ru_utime + usage.ru_stime


def _child(tree, body, cap, r, w):
    try:
        os.setpgid(0, 0)
        os.close(r)
        null = os.open(os.devnull, os.O_RDWR)
        os.dup2(null, 0)
        os.dup2(null, 1)  # stdout carries the answers: nothing else may reach it
        os.dup2(null, 2)
        limit = int(cap) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (limit, limit + 1))
        body(tree, w)
    except BaseException:  # noqa: BLE001
        try:
            os.write(w, b"F")
        except OSError:
            pass
    finally:
        os._exit(0)


class UnitRunner:
    def __init__(self, src, units, n_commits):
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        self.src = src
        self.n = units["n"]
        self.unit_ms = units["ms"]
        self.fixed_ms = units["fixed_ms"]
        self.fixed_rows_ms = units.get("fixed_rows_ms")
        self._frames_blob = units["frames"]
        self.frames = None  # loaded before the first run: a batch that runs none skips pandas
        # CPU here per recorded CPU, corrected by each finished run. A host 3x slower than the
        # recording one makes a full run cost more than the test it saves, so it measures itself.
        self.scale = min(8.0, max(0.3, units["scale"] * bench() / BENCH_REF_S))
        self.deadline = STARTED + min(MAX_WALL, SECONDS_PER_COMMIT * n_commits)
        self.cpu = 0.0

    def estimate(self, keep, fast=False):
        return ((self.fixed_rows_ms if fast else self.fixed_ms) + sum(self.unit_ms[i] for i in keep)) * self.scale / 1000

    def passes(self, files, keep, saving_s):
        """Runs the queries in `keep` under the patch when the expected saving
        beats the expected CPU. True only for a finished, clean pass."""
        fast = self.fixed_rows_ms is not None and rows_ok(files)
        est = self.estimate(keep, fast)
        if saving_s <= WORTH * est:
            return False, f"not worth it ({len(keep)} queries ~{est:.1f}s for {saving_s:.1f}s)"
        cap = min(saving_s, 2.5 * est + 2)
        wall = 1.5 * cap + 2
        if time.monotonic() + wall > self.deadline:
            return False, "out of time"
        if self.frames is None:
            # loaded once here, inherited by every fork
            import unittest  # noqa: F401

            import duckdb  # noqa: F401
            import numpy  # noqa: F401
            import pandas.testing  # noqa: F401

            self.frames = pickle.loads(self._frames_blob)
        # cheapest first: a failing run stops at its first failing query
        keep = sorted(keep, key=lambda i: self.unit_ms[i])
        verdict, cpu = self._fork(files, keep, cap, wall, load_rows() if fast else None)
        self.cpu += cpu
        if verdict == b"P":
            self.scale = min(8.0, max(0.3, (self.scale + self.scale * cpu / est) / 2))
        elif not verdict and cpu > est:  # killed at its cap: this host is at least that slow
            self.scale = min(8.0, self.scale * cpu / est)
        return verdict == b"P", f"{len(keep)} queries ran {cpu:.1f}s (~{est:.1f}s): {verdict.decode() or 'no verdict'}"

    def _fork(self, files, keep, cap, wall, rows):
        copy = TABLE_PY in files
        out, cpu = fork_run(self.src, files, lambda tree, w: self._child(tree, keep, w, rows, copy), cap, wall, lambda buf: bool(buf))
        return out[:1], cpu

    def _child(self, tree, keep, w, rows, copy):
        ok = run_units(tree, keep, self.frames, self.n, rows=rows, copy_init=copy)
        os.write(w, b"P" if ok else b"F")


SUITE_WORTH = 3.0  # run when dropping the selection saves this many times the run's CPU: 1 in 2 to 4 runs passes on the labeled sets
SUITE_KEEP_WORTH = 2.0  # after a failure, run on while the tests left can still save this many times their CPU


# What the suite's tests import besides sqlglot and themselves, loaded once in the selector so
# every fork starts with it: about 40 ms a run, and 200 ms for a file that loads pandas and duckdb
WARM = (
    "argparse ast asyncio base64 calendar concurrent.futures contextvars copy csv ctypes dataclasses datetime "
    "decimal difflib dis email fractions gzip hashlib heapq inspect ipaddress json logging multiprocessing "
    "numbers pathlib pkgutil platform pprint queue secrets shlex sqlite3 statistics string subprocess tarfile "
    "textwrap threading tokenize tomllib traceback typing unicodedata unittest unittest.mock urllib uuid "
    "xml.etree.ElementTree zipfile zoneinfo dateutil mypy_extensions pytz pytest"
).split()
HEAVY = ("numpy", "duckdb", "pandas.testing")
HEAVY_FILES = ("tests/test_executor.py", "tests/test_optimizer.py")  # the test files that import HEAVY
EXECUTOR = "TestExecutor"


def warm(names):
    import importlib
    import pkgutil

    for name in names:
        try:
            mod = importlib.import_module(name)
        except Exception:  # a module this image lacks: the run imports it or fails as the suite would
            continue
        if name == "pytest":
            import _pytest

            for info in pkgutil.walk_packages(_pytest.__path__, "_pytest."):
                if info.name != "_pytest.debugging":  # pdb pulls readline, which touches the terminal
                    try:
                        importlib.import_module(info.name)
                    except Exception:
                        pass
        del mod


def run_suite(root, ids, w, rows=None):
    """Runs the tests `ids` (pytest node ids, in that order) with pytest on the
    tree at `root`, writing "P <i>" or "F <i>" per test as it finishes, "D" at
    the end, "X" when the run can't stand for the suite's. With `rows`,
    TestExecutor's setUpClass runs through setup_executor."""
    import pytest

    # the patched tree must be imported fresh, and pools sized as in the labeling runs
    if any(m == "sqlglot" or m.startswith(("sqlglot.", "tests.")) or m == "tests" for m in sys.modules) or os.process_cpu_count() > 2:
        os.write(w, b"X\n")
        return
    os.chdir(root)
    sys.path.insert(0, root)
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONPATH"] = root  # subprocesses a test starts (test_lazy_load) import the patched tree
    order = {nid: i for i, nid in enumerate(ids)}

    class Verdicts:
        def __init__(self):
            self.bad = set()
            self.broken = False

        @pytest.hookimpl(tryfirst=True)
        def pytest_pycollect_makeitem(self, collector, name, obj):
            # before pytest's unittest plugin collects the class, which keeps its setUpClass
            if rows is not None and name == EXECUTOR and getattr(obj, "__module__", None) == "tests.test_executor":
                setup = obj.setUpClass
                obj.setUpClass = staticmethod(lambda: setup_executor(sys.modules["tests.test_executor"], rows, setup))

        def pytest_collectreport(self, report):
            if report.failed:
                self.broken = True

        def pytest_collection_modifyitems(self, session, config, items):
            if sorted(it.nodeid for it in items) != sorted(ids):
                self.broken = True
            if self.broken:
                os.write(w, b"X\n")
                items[:] = []
                return
            items.sort(key=lambda it: order[it.nodeid])

        def pytest_runtest_logreport(self, report):
            # a subtest's failure arrives as its own report beside the test's passing one
            if not report.passed:
                self.bad.add(report.nodeid)
            if report.when == "teardown" and report.nodeid in order:
                tag = "F" if report.nodeid in self.bad else "P"
                os.write(w, f"{tag} {order[report.nodeid]}\n".encode())

        @pytest.hookimpl(trylast=True)
        def pytest_sessionfinish(self, session, exitstatus):
            # every verdict is in: the summary and pytest's closing gc.collect() (~40 ms) would only cost
            if not self.broken and exitstatus in (0, 1):
                os.write(w, b"D\n")
                os._exit(0)

    # --tb=no: a failure's traceback reads source files to show it, a third of a failing run's CPU
    pytest.main(["-p", "no:cacheprovider", "-q", "--tb=no", "--rootdir", root, *ids], plugins=[Verdicts()])


def load_suite_cpu(maps_dir):
    with open(os.path.join(maps_dir, "testcpu.json")) as f:
        return json.load(f)


class SuiteRunner:
    """Runs a commit's selected tests under its patch, so the answer can drop
    the ones that pass cleanly."""

    def __init__(self, src, cpu_map, ids, files_of, n_commits):
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # the scorer's image has no pytest plugins
        self.src = src
        self.ids = ids
        self.files_of = files_of
        self.file_s = cpu_map["file"]
        self.test_s = {int(k): v for k, v in cpu_map["test"].items()}
        self.base_s = min(self.file_s.values())
        self.warm_s = cpu_map["warm"]
        self.heavy_s = cpu_map["heavy"]
        self.class_s = {}
        for n, nid in ids.items():
            c = nid.rsplit("::", 1)[0]
            self.class_s[c] = min(self.class_s.get(c, 9.0), self.test_est(n))
        self.scale = min(8.0, max(0.3, bench() / cpu_map["bench"]))
        self.deadline = STARTED + min(MAX_WALL, SECONDS_PER_COMMIT * n_commits)
        self.cpu = 0.0
        self.runs = 0

    def test_est(self, n):
        """Recorded CPU of test n past its file's collection, on the recording machine."""
        return max(0.0, self.test_s.get(n, 9.0) - self.file_s.get(self.files_of[n], 9.0))

    def estimate(self, nums):
        """Recorded CPU of one run of `nums`, scaled to this host: the imports
        the selector hasn't loaded yet, each file's collection once, each class's
        setup once (its cheapest test alone, past collection), each test past that."""
        files = {self.files_of[n] for n in nums}
        if not files:
            return 0.0
        fixed = self.base_s + sum(self.file_s.get(f, 9.0) - self.base_s for f in files)
        if "pytest" not in sys.modules:
            fixed += self.warm_s
        if files & set(HEAVY_FILES) and "pandas" not in sys.modules:
            fixed += self.heavy_s
        classes = {self.ids[n].rsplit("::", 1)[0] for n in nums}
        setup = sum(self.class_s[c] for c in classes)
        return (fixed + setup + sum(self.test_est(n) - self.class_s[self.ids[n].rsplit("::", 1)[0]] for n in nums)) * self.scale

    def order(self, nums):
        """Cheapest first, a class's tests together so its class fixture is set up once."""
        by_cls = {}
        for n in nums:
            by_cls.setdefault(self.ids[n].rsplit("::", 1)[0], []).append(n)
        groups = [sorted(g, key=lambda n: (self.test_est(n), n)) for g in by_cls.values()]
        groups.sort(key=lambda g: (self.test_est(g[0]), g[0]))
        return [n for g in groups for n in g]

    def prove(self, files, nums, cost_of):
        """The tests in `nums` that didn't pass cleanly under the patch, or None
        when nothing ran or the run can't vouch for any pass. cost_of(tests)
        is the answer's simulated ms for that set."""
        gain = (cost_of(nums) - cost_of([])) / 1000
        est = self.estimate(nums)
        if gain <= SUITE_WORTH * est:
            return None, f"not worth it ({len(nums)} tests ~{est:.2f}s for {gain:.1f}s)"
        cap = min(gain, 3 * est + 1)
        wall = 1.5 * cap + 2
        if time.monotonic() + wall > self.deadline:
            return None, "out of time"
        # loaded here, inherited by every fork
        t0 = time.process_time()
        warm(WARM)
        if {self.files_of[n] for n in nums} & set(HEAVY_FILES):
            warm(HEAVY)
        self.cpu += time.process_time() - t0
        seq = self.order(nums)
        stop = []

        def done(buf):
            lines = buf.split(b"\n")[:-1]
            if not lines or lines[-1] in (b"D", b"X") or lines[-1][:1] not in (b"P", b"F"):
                return bool(lines)
            if lines[-1][:1] == b"P":
                return False
            failed = [seq[i] for i, x in enumerate(lines) if x[:1] == b"F"]
            rest = seq[len(lines):]
            gain = cost_of(failed + rest) - cost_of(failed)
            if gain / 1000 <= SUITE_KEEP_WORTH * self.estimate(rest):
                stop.append(len(lines))
                return True
            return False

        rows = load_rows() if rows_ok(files) and any(self.ids[n].split("::")[:2] == ["tests/test_executor.py", EXECUTOR] for n in seq) else None
        out, cpu = fork_run(self.src, files, lambda tree, w: run_suite(tree, [self.ids[n] for n in seq], w, rows), cap, wall, done)
        self.cpu += cpu
        self.runs += 1
        lines = out.split(b"\n")[:-1]
        clean = bool(lines) and lines[-1] == b"D"
        if clean:
            lines = lines[:-1]
        elif stop:
            lines = lines[: stop[0]]
        # a pass counts only from a run that finished, or that the parent stopped
        # after a failure; anything else (crash, cap, timeout, X) keeps the selection
        if (clean and len(lines) == len(seq)) or stop:
            if all(x == f"{x[:1].decode()} {i}".encode() and x[:1] in (b"P", b"F") for i, x in enumerate(lines)):
                passed = {seq[i] for i, x in enumerate(lines) if x[:1] == b"P"}
                if clean:  # a stopped child runs past the stop: its CPU outweighs the prefix's estimate
                    # half a step in log space: an arithmetic mean of noisy factors climbs (x3 then /3 nets 1.33x)
                    ran = self.estimate(seq)  # the imports are loaded now: the child's share only
                    self.scale = min(8.0, max(0.3, self.scale * (cpu / ran) ** 0.5))
                left = [n for n in nums if n not in passed]
                return left, f"{len(nums)} tests ran {cpu:.2f}s (~{est:.2f}s): {len(passed)} passed, {len(left)} kept"
        if cpu > est:
            self.scale = min(8.0, self.scale * cpu / est)
        return None, f"{len(nums)} tests ran {cpu:.2f}s (~{est:.2f}s): no verdict ({out[-40:]!r})"


if __name__ == "__main__":  # the image build: subrun.py <sqlglot tree>
    save_rows(sys.argv[1], ROWS)
