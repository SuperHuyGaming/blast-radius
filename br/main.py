"""Blast Radius selector: pick candidate tests per patch, run the cheap ones against the
patched tree, and print only the tests that fail (plus expensive tests judged risky).

Everything printed to stdout is an answer line; logs go to stderr.
"""

import json
import os
import pickle
import sys
import time

try:
    import resource
except ImportError:  # windows dev
    resource = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import brsel as sel  # noqa: E402
import exec_cache  # noqa: E402
from brsel import changed_old_lines  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
SRC = os.environ.get("BR_SRC", "/app/sqlglot")
INDEX = os.environ.get("BR_INDEX", os.path.join(HERE, "index.pkl"))
EXPENSIVE = float(os.environ.get("BR_EXPENSIVE", "1.0"))
T0 = time.time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def bits(b):
    out = []
    while b:
        low = b & -b
        out.append(low.bit_length() - 1)
        b ^= low
    return out


# ------------------------------------------------------------------ planning
class Plan:
    __slots__ = ("cid", "run", "listed", "patched", "deleted", "notes", "maybe_exp", "slices", "stop_early", "must", "import_check")

    def __init__(self, cid):
        self.cid = cid
        self.run = 0  # bitset (index order) of tests to execute
        self.listed = 0  # bitset of tests to list without running
        self.patched = {}  # relpath -> new text
        self.deleted = set()
        self.notes = []
        self.maybe_exp = 0  # expensive candidates (decided after the cheap run)
        self.slices = {}  # test index -> {fixture file: set of entry indexes}
        self.stop_early = False  # stop verifying at the first failure (see decide)
        self.must = 0  # readers of changed data files: almost always fail, never dropped
        self.import_check = False  # import the patched modules even if no test is run


def read_src(rel):
    try:
        with open(os.path.join(SRC, rel), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def tests_under(idx, d):
    b = 0
    for f, ids in idx["test_ids_by_file"].items():
        if f.startswith(d):
            for i in ids:
                b |= 1 << i
    return b


PYTEST_CONFIG = ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini")


def plan_commit(cid, text, idx, cache):
    p = Plan(cid)
    sel.ENUM_NAMES = idx.get("enum_names", set())
    line_info = idx["line_info"]
    test_line = idx["test_line"]
    reads = idx["reads"]
    cand = 0
    fixture_only = 0
    line_cand = 0  # tests selected only through runtime statements (sliceable)
    other = 0  # tests selected for any other reason (must run in full)
    nover = 0  # tests our runner cannot verify faithfully: report them
    edited = 0  # numbered tests whose own source changed
    changed = {}  # file -> set of old line numbers in affected statements
    expand_all = idx.get("expand", {})
    for fp in sel.parse_patch(text):
        path = fp.new if not fp.is_deleted else fp.old
        if path is None:
            continue
        old_b = None if fp.is_new else read_src(fp.old or path)
        if fp.binary:
            # the overlay cannot apply binary bytes: report the readers unverified
            p.notes.append("binary " + path)
            nover |= reads.get(path, 0) | reads.get(fp.old or path, 0)
            continue
        if fp.is_deleted:
            p.deleted.add(path)
            new_text = None
        else:
            old_lines = [] if old_b is None else old_b.decode("utf-8", "surrogateescape").split("\n")
            trailing = bool(old_lines) and old_lines[-1] == ""
            if trailing:
                old_lines = old_lines[:-1]
            new_lines = sel.apply_file(old_lines, fp)
            if new_lines is None:
                p.notes.append("apply-failed " + path)
                # fall back to treating the whole file as changed
                new_text = None
            else:
                new_text = "\n".join(new_lines) + ("\n" if (trailing or fp.is_new) else "")
                p.patched[path] = new_text
        renamed = bool(fp.old and fp.new and fp.old != fp.new and not fp.is_new)
        if renamed:
            # rename: old path disappears; its readers, users and importers are affected
            p.deleted.add(fp.old)
            p.import_check = True
            cand |= reads.get(fp.old, 0) | idx["module_users"].get(fp.old, 0)
            cand |= idx["importers"].get(fp.old, 0)
            for i in idx["test_ids_by_file"].get(fp.old, ()):
                cand |= 1 << i
        if fp.is_deleted:
            cand |= reads.get(path, 0)

        is_py = path.endswith(".py")
        base = path.rsplit("/", 1)[-1]
        if base in PYTEST_CONFIG and "/" not in path:
            added = " ".join(t for h in fp.hunks for g, t in h[4] if g in "+-")
            if "pytest" in added or "[tool" in added:
                p.notes.append("pytest-config " + path)
                nover |= tests_under(idx, "tests/")
            continue
        if base == "conftest.py" and not (
            new_text is not None
            and old_b is not None
            and sel.ast_equal(old_b, new_text.encode("utf-8", "surrogateescape"))
        ):
            p.notes.append("conftest " + path)
            nover |= tests_under(idx, path.rsplit("/", 1)[0] + "/" if "/" in path else "tests/")
            continue
        if renamed:
            pass
        elif is_py and new_text is not None and old_b is not None and sel.trivial_text_change(fp):
            if sel.ast_equal(old_b, new_text.encode("utf-8", "surrogateescape")):
                cand |= reads.get(path, 0)  # tests reading the source as text
                p.notes.append("ast-equal " + path)
                continue
        if not renamed and is_py and path.startswith("sqlglot/") and new_text is not None and old_b is not None:
            if sel.equivalent_compare(fp, old_b, new_text.encode("utf-8", "surrogateescape")):
                cand |= reads.get(path, 0)
                p.notes.append("equiv-compare " + path)
                continue

        touched = changed_old_lines(fp)
        if path.startswith("sqlglot/") and is_py:
            info = line_info.get(path)
            broken = new_text is None and not fp.is_deleted
            if new_text is not None:
                try:
                    # bytes, so an encoding cookie is honoured as the import system would
                    compile(new_text.encode("utf-8", "surrogateescape"), path, "exec")
                except (SyntaxError, ValueError):
                    broken = True
            if info is None or broken or fp.is_deleted or fp.is_new:
                p.import_check = True
                p.notes.append("module-wide " + path)
                cand |= idx["module_users"].get(path, 0)
                # a child always starts for module-level file changes, so the patched
                # tree's import check runs (e.g. a new package shadowing a module)
                cand |= idx["special"].get("lazy_load", 0)
                if info:
                    for b in info:
                        cand |= b
                if fp.is_new:
                    d = path.rsplit("/", 1)[0] + "/"
                    for r, b in reads.items():
                        if r.startswith(d):
                            cand |= b
                    if path.endswith("/__init__.py"):
                        # a new package shadows a same-named module: that module is gone
                        shadowed = path[: -len("/__init__.py")] + ".py"
                        if shadowed in line_info:
                            p.notes.append("shadows " + shadowed)
                            cand |= idx["module_users"].get(shadowed, 0) | reads.get(shadowed, 0)
                            cand |= idx["importers"].get(shadowed, 0)
                            for b in line_info[shadowed]:
                                cand |= b
            else:
                ex = expand_all.get(path)
                for l in touched:
                    if 0 < l < len(info):
                        b = info[l]
                        cand |= b
                        e = ex[l] if ex is not None and l < len(ex) else None
                        if e is not None:
                            line_cand |= b
                            changed.setdefault(path, set()).update(range(e[0], e[1] + 1))
                        else:
                            other |= b
                if touched & idx["import_lines"].get(path, set()):
                    p.import_check = True  # an import-time line can break every importer
                # import-time lines also affect the lazy-load subprocess test
                if path in idx["eager_files"] and touched & idx["import_lines"].get(path, set()):
                    cand |= idx["special"].get("lazy_load", 0)
                # new statements at class or module level (a new method, attribute or
                # import) change the class/module itself, not any existing line
                shallow = sel.shallow_insertions(fp)
                if shallow:
                    p.import_check = True
                    b = sel.class_or_module_users(old_b, shallow, info, idx["module_users"].get(path, 0))
                    cand |= b
                    other |= b
                    if path in idx["eager_files"]:
                        cand |= idx["special"].get("lazy_load", 0)
            cand |= reads.get(path, 0)
        elif path.startswith("tests/") and is_py:
            info = test_line.get(path)
            if base == "__init__.py":
                # package code runs before every test module below it
                p.notes.append("test-package " + path)
                cand |= tests_under(idx, path.rsplit("/", 1)[0] + "/")
                other |= tests_under(idx, path.rsplit("/", 1)[0] + "/")
            elif info is None:
                p.notes.append("unknown-test-file " + path)
                cand |= idx["importers"].get(path, 0)
            else:
                tb = 0
                for l in touched:
                    if 0 < l < len(info):
                        tb |= info[l]
                    elif l >= len(info) and info:
                        tb |= info[-1]
                added = " ".join(t for h in fp.hunks for g, t in h[4] if g in "+-")
                edited |= tb
                if any(k in added for k in ("pytest", "Pool", "spawn", "mp_context", "get_context")):
                    # pytest-only semantics or real process pools: our unittest runner
                    # cannot reproduce them, so report these tests unverified
                    nover |= tb
                else:
                    cand |= tb
                cand |= idx["importers"].get(path, 0)
                other |= tb | idx["importers"].get(path, 0)
            cand |= reads.get(path, 0)
        elif path.startswith("sqlglotc/"):
            # these tests resolve their own path out of the overlay: list, don't verify
            nover |= idx["special"].get("sqlglotc", 0)
        else:
            if path in reads:
                fixture_only |= reads[path]
            else:
                p.notes.append("ignored " + path)
    p.listed |= nover
    cand &= ~nover
    fixture_only &= ~nover
    if cand:
        p.run = cand | fixture_only
        p.must = fixture_only | (edited & cand)
        other |= fixture_only | (cand & ~line_cand)
        units = idx.get("units", {})
        for i in bits(line_cand & ~other):
            u = units.get(i)
            if u is None:
                continue
            base = u["base"]
            gaps = u.get("gaps", {})
            if any(changed[f] & base.get(f, set()) for f in changed):
                continue  # shared code path of the test: run it in full
            if any(changed[f] & gaps.get(f, set()) for f in changed):
                continue  # reached only when entries run together: run it in full
            k = 0
            for f, ls in changed.items():
                inv = u["inv"].get(f)
                if inv:
                    for l in ls:
                        k |= inv.get(l, 0)
            if not k:
                continue  # only the whole-file run reached it: run in full
            sl = {}
            for j in bits(k):
                fn, e = u["keys"][j]
                sl.setdefault(fn, set()).add(e)
            for fn in {fn for fn, _ in u["keys"]}:
                sl.setdefault(fn, set())
            p.slices[i] = sl
    else:
        # only data files changed: list their readers without importing anything
        p.listed |= fixture_only
    return p


# ------------------------------------------------------------------ execution (child side)
def child_run(plan, test_ids, seconds_by_idx, conn_w):
    """Runs inside a forked child: overlay tree, import, run tests, send failing set."""
    import shutil
    import signal
    import traceback
    import unittest

    if os.name != "nt":
        os.dup2(2, 1)  # anything printed goes to stderr
    root = os.path.join(os.environ.get("TMPDIR", "/tmp"), "ov_" + plan.cid)
    failing = set()
    stopped = False
    broken = False
    pool_classes = set()
    try:
        build_overlay(root, plan)
        os.chdir(root)
        sys.path.insert(0, root)
        sys.dont_write_bytecode = True
        install_sync_executor()
        # import every patched sqlglot module first: an import-time error (bad default
        # argument, shadowing package, encoding) breaks far more than the candidates
        for rel in list(plan.patched) + list(plan.deleted):
            if rel.startswith("sqlglot/") and rel.endswith(".py"):
                name = rel[:-3].replace("/", ".")
                if name.endswith(".__init__"):
                    name = name[: -len(".__init__")]
                try:
                    __import__("sqlglot")
                    if rel not in plan.deleted:
                        __import__(name)
                except BaseException:
                    log("patched module import failed", name, traceback.format_exc(limit=2))
                    broken = True
                    raise
        filt = install_fixture_filter() if plan.slices else None

        by_mod = {}
        for i in bits(plan.run):
            tid = test_ids[i]
            f, rest = tid.split("::", 1)
            cls, meth = rest.split("::", 1) if "::" in rest else (rest, None)
            by_mod.setdefault(f, {}).setdefault(cls, []).append((meth, i))

        class Res(unittest.TestResult):
            def __init__(self):
                super().__init__()
                self.bad = set()

            def _mark(self, test):
                if plan.stop_early:
                    self.stop()
                i = getattr(test, "_br_idx", None)
                if i is None:
                    # class/module fixture error: mark the whole class
                    for j in getattr(self, "_cur_class_idx", []):
                        self.bad.add(j)
                else:
                    self.bad.add(i)

            def addError(self, test, err):
                self._mark(test)

            def addFailure(self, test, err):
                self._mark(test)

            def addSubTest(self, test, subtest, err):
                if err is not None:
                    self._mark(test)

            def addUnexpectedSuccess(self, test):
                self._mark(test)

        def on_alarm(signum, frame):
            raise TimeoutError("br timeout")

        has_alarm = hasattr(signal, "SIGALRM")
        if has_alarm:
            signal.signal(signal.SIGALRM, on_alarm)

        for f, classes in by_mod.items():
            modname = f[:-3].replace("/", ".")
            try:
                mod = __import__(modname, fromlist=["*"])
            except BaseException:
                log("import error", modname, traceback.format_exc(limit=3))
                # the whole file fails to import: every numbered test in it fails
                failing.update(i for i, t in enumerate(test_ids) if t.startswith(f + "::"))
                continue
            if os.environ.get("BR_IMPORT_ONLY"):
                continue
            if f + "::TestExecutor" == exec_cache.CLASS and "TestExecutor" in classes and exec_ok(plan):
                rows = load_exec_rows()
                if rows is not None:
                    try:
                        exec_cache.install(mod, rows)
                    except BaseException:
                        log("exec cache install failed", traceback.format_exc(limit=2))
            for cls, lst in classes.items():
                C = getattr(mod, cls, None)
                if C is None:
                    for meth, i in lst:
                        failing.add(i)
                    continue
                res = Res()
                # failfast makes a failing subTest abort its test method (one failure is
                # enough); our add* overrides never call stop(), so the suite continues.
                res.failfast = True
                res._cur_class_idx = [i for _, i in lst]
                suite = unittest.TestSuite()
                for meth, i in lst:
                    try:
                        t = C(meth)
                    except BaseException:
                        failing.add(i)
                        continue
                    t._br_idx = i
                    if filt is not None:
                        t.run = filt.wrap(t, C, plan.slices.get(i))
                    suite.addTest(t)
                budget = sum(10 * seconds_by_idx.get(i, 0.1) + 3 for _, i in lst)
                if has_alarm:
                    signal.setitimer(signal.ITIMER_REAL, budget)
                try:
                    suite.run(res)
                except BaseException:
                    log("suite crash", cls, traceback.format_exc(limit=3))
                    failing.update(i for _, i in lst)
                finally:
                    if has_alarm:
                        signal.setitimer(signal.ITIMER_REAL, 0)
                    try:
                        unittest.TestSuite._tearDownPreviousClass(suite, None, res)
                    except BaseException:
                        pass
                failing |= res.bad
                if POOL_BROKEN[0]:
                    pool_classes.add(f + "::" + cls)
                    POOL_BROKEN[0] = False
                if plan.stop_early and failing:
                    stopped = True
                    break
            if stopped:
                break
    except BaseException:
        log("child crash", plan.cid, traceback.format_exc())
        failing = set(bits(plan.run))
    try:
        conn_w.write(pickle.dumps({"failing": failing, "stopped": stopped, "broken": broken, "pool": pool_classes}))
        conn_w.close()
    except BaseException:
        pass
    try:
        shutil.rmtree(root, ignore_errors=True)
    except BaseException:
        pass


CLASS_LISTS = {"tpch_sqls": "optimizer/tpc-h/tpc-h.sql", "tpcds_sqls": "optimizer/tpc-ds/tpc-ds.sql"}


class _Filter:
    """Per-test filtering of fixture loaders to the entries a patch can affect."""

    def __init__(self):
        self.allowed = None  # None = everything; else {filename: set of entry indexes}
        self.full = {}

    def gen(self, orig):
        def wrapped(filename):
            allowed = self.allowed
            if allowed is None or filename not in allowed:
                yield from orig(filename)
                return
            keep = allowed[filename]
            for k, item in enumerate(orig(filename)):
                if k in keep:
                    yield item

        return wrapped

    def wrap(self, test, C, sl):
        orig_run = test.run

        def run(result=None):
            for a in CLASS_LISTS:
                if a in C.__dict__ and a not in self.full:
                    self.full[a] = list(C.__dict__[a])
            self.allowed = sl
            for a, fn in CLASS_LISTS.items():
                if a in self.full:
                    lst = self.full[a]
                    if sl is not None and fn in sl:
                        setattr(C, a, [lst[k] for k in sorted(sl[fn]) if k < len(lst)])
                    else:
                        setattr(C, a, lst)
            try:
                return orig_run(result)
            finally:
                self.allowed = None
                for a in CLASS_LISTS:
                    if a in self.full:
                        setattr(C, a, self.full[a])

        return run


def install_fixture_filter():
    import tests.helpers as H

    f = _Filter()
    H.load_sql_fixture_pairs = f.gen(H.load_sql_fixture_pairs)
    H.load_sql_fixtures = f.gen(H.load_sql_fixtures)
    return f


def build_overlay(root, plan):
    """Symlink farm of SRC with patched files written as real files."""
    patched = plan.patched
    deleted = plan.deleted
    dirs_needed = set()
    for rel in list(patched) + list(deleted):
        parts = rel.split("/")
        for k in range(1, len(parts)):
            dirs_needed.add("/".join(parts[:k]))
    use_links = hasattr(os, "symlink") and os.name != "nt"

    def mk(src_dir, dst_dir, rel):
        os.makedirs(dst_dir, exist_ok=True)
        try:
            entries = os.listdir(src_dir)
        except OSError:
            entries = []
        names = set(entries)
        # new files / dirs in this directory
        for p in list(patched) + list(dirs_needed):
            parent, _, name = p.rpartition("/")
            if parent == rel and name not in names:
                names.add(name)
        for name in sorted(names):
            r = name if not rel else rel + "/" + name
            s = os.path.join(src_dir, name)
            d = os.path.join(dst_dir, name)
            if r in deleted:
                continue
            if r in patched:
                with open(d, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                    fh.write(patched[r])
                continue
            if r in dirs_needed:
                mk(s, d, r)
                continue
            if use_links:
                os.symlink(s, d)
            else:
                import shutil

                if os.path.isdir(s):
                    shutil.copytree(s, d)
                else:
                    shutil.copy2(s, d)

    mk(SRC, root, "")


POOL_BROKEN = [False]


def install_sync_executor():
    import concurrent.futures as cf
    import concurrent.futures.process  # noqa: F401
    import pickle as pk

    class SyncExecutor:
        def __init__(self, *a, initializer=None, initargs=(), **k):
            if initializer is not None:
                initializer(*pk.loads(pk.dumps(initargs)))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        _broken = False

        def submit(self, fn, *a, **k):
            # A real pool breaks for good when a call or a result cannot be unpickled
            # on the other side; every later submit then raises BrokenProcessPool.
            f = cf.Future()
            if self._broken:
                f.set_exception(cf.process.BrokenProcessPool("pool broken"))
                return f
            try:
                blob = pk.dumps((a, k))
            except BaseException as e:
                f.set_exception(e)
                return f
            try:
                a, k = pk.loads(blob)
            except BaseException:
                return self._break(f)
            try:
                rb = pk.dumps(fn(*a, **k))
            except BaseException as e:
                f.set_exception(e)
                return f
            try:
                f.set_result(pk.loads(rb))
            except BaseException:
                return self._break(f)
            return f

        def _break(self, f):
            self._broken = True
            POOL_BROKEN[0] = True
            f.set_exception(cf.process.BrokenProcessPool("pool broken"))
            return f

        def shutdown(self, *a, **k):
            pass

    cf.ProcessPoolExecutor = SyncExecutor


# ------------------------------------------------------------------ worker split
def split(nums, dur_ms):
    """Minimise max load + 2000 ms per worker over 1..8 workers (LPT)."""
    if not nums:
        return []
    order = sorted(nums, key=lambda n: -dur_ms[n])
    best = None
    for k in range(1, min(8, len(order)) + 1):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads))
            loads[j] += dur_ms[n]
            ws[j].append(n)
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]


# ------------------------------------------------------------------ main
def main():
    with open(INDEX, "rb") as fh:
        idx = pickle.load(fh)
    with open(os.path.join(DATA, "tests.json")) as fh:
        tj = json.load(fh)
    tests = tj["tests"]
    num_by_id = {t["id"]: t["number"] for t in tests}
    dur_ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    test_ids = idx["tests"]
    sec_idx = {}
    for i, tid in enumerate(test_ids):
        n = num_by_id.get(tid)
        if n is not None:
            sec_idx[i] = dur_ms[n] / 1000.0
    exp_mask = 0
    for i, s in sec_idx.items():
        if s >= EXPENSIVE:
            exp_mask |= 1 << i
    known = 0
    for i in sec_idx:
        known |= 1 << i

    cost = load_cost()
    KNOWN_IDS.append([i for i in range(len(test_ids)) if i in sec_idx])
    TEST_IDS.append(test_ids)
    cdir = os.path.join(DATA, "commits")
    plans = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        with open(os.path.join(cdir, name), "rb") as fh:
            text = fh.read().decode("utf-8", "surrogateescape")
        try:
            p = plan_commit(name[:-6], text, idx, None)
        except Exception as e:  # never crash: be conservative
            log("plan error", name, repr(e))
            p = Plan(name[:-6])
            p.run = known
        p.run &= known
        p.listed &= known
        decide(p, idx, test_ids, sec_idx, exp_mask, cost)
        plans.append(p)
    log("planned %d commits in %.2fs" % (len(plans), time.time() - T0))

    results = {}
    todo = [p for p in plans if p.run or p.import_check]
    for p in plans:
        if not p.run and not p.import_check:
            results[p.cid] = set()
    run_children(todo, test_ids, sec_idx, results)

    for p in plans:
        failing = results.get(p.cid, set())
        sel_bits = p.listed
        for i in failing:
            sel_bits |= 1 << i
        if p.maybe_exp:
            if failing or not p.run:
                sel_bits |= p.maybe_exp
        nums = sorted({num_by_id[test_ids[i]] for i in bits(sel_bits) if test_ids[i] in num_by_id})
        log(p.cid, "ran", len(bits(p.run)), "sliced", len(p.slices), "fail", len(failing), "exp", len(bits(p.maybe_exp)), "->", len(nums), " ".join(p.notes)[:200])
        sys.stdout.write(json.dumps({"commit": p.cid, "workers": split(nums, dur_ms)}) + "\n")
    sys.stdout.flush()
    log("total %.2fs" % (time.time() - T0))


KNOWN_IDS = []
TEST_IDS = []
EXEC_ROWS = []  # [rows] once loaded: TestExecutor's parsed CSV fixtures (see exec_cache.py)
EXEC_SETUP_CACHED = 0.05


def exec_ok(p):
    return exec_cache.guard_ok(list(p.patched) + list(p.deleted))


def load_exec_rows():
    if not EXEC_ROWS:
        try:
            with open(os.path.join(HERE, "..", "data", "exec_rows.pkl"), "rb") as fh:
                EXEC_ROWS.append(pickle.load(fh))
        except Exception as e:
            log("no exec rows", e)
            EXEC_ROWS.append(None)
    return EXEC_ROWS[0]


def unpack(msg, p):
    """Child message -> set of tests to report. After an early stop every candidate is
    reported: the expensive test that a failure implies sets a peak they fit under."""
    if msg.get("broken"):
        # a patched module cannot be imported: report every known test
        return set(KNOWN_IDS[0]) if KNOWN_IDS else set(bits(p.run))
    out = set(msg["failing"])
    if msg.get("pool"):
        # a broken shared pool fails every later user of it, run or not: report the class
        for i in KNOWN_IDS[0] if KNOWN_IDS else ():
            if TEST_IDS and TEST_IDS[0][i].rsplit("::", 1)[0] in msg["pool"]:
                out.add(i)
    if msg.get("stopped"):
        return out | set(bits(p.run)) | set(bits(p.maybe_exp))
    return out


def load_cost():
    try:
        with open(os.path.join(HERE, "..", "data", "runcost.json")) as fh:
            return json.load(fh)
    except OSError:
        return {"import": {}, "class_setup": {}, "own": {}}


CHILD_BASE = 0.12  # fork + overlay + importing patched sqlglot (measured ~0.1 s)


def est_cost(p, idx, test_ids, run_bits, cost):
    """Estimated CPU seconds to run `run_bits` in one child."""
    own = cost["own"]
    imp = cost["import"]
    setup = cost["class_setup"]
    units = idx.get("units", {})
    mods = set()
    classes = set()
    total = CHILD_BASE
    for i in bits(run_bits):
        tid = test_ids[i]
        mods.add(tid.split("::")[0])
        classes.add(tid.rsplit("::", 1)[0])
        c = own.get(tid, 0.05)
        sl = p.slices.get(i)
        if sl is not None and i in units:
            n = max(1, len(units[i]["keys"]))
            k = sum(len(v) for v in sl.values())
            c = c * k / n + 0.003
        total += c
    total += sum(max(0.0, imp.get(m, 0.1) - 0.08) for m in mods) + 0.08 * bool(mods)
    cached = exec_ok(p) and os.path.exists(os.path.join(HERE, "..", "data", "exec_rows.pkl"))
    total += sum(EXEC_SETUP_CACHED if (cached and c == exec_cache.CLASS) else setup.get(c, 0.0) for c in classes)
    return total


def sched_cost(b, sec_idx):
    """Schedule cost (s) of reporting the tests in bitset b: LPT over 1..8 workers."""
    ds = sorted((round(sec_idx.get(i, 0.0) * 1000) for i in bits(b)), reverse=True)
    if not ds:
        return 0.0
    best = None
    for k in range(1, min(8, len(ds)) + 1):
        loads = [0] * k
        for d in ds:
            loads[loads.index(min(loads))] += d
        c = max(loads) + 2000 * k
        best = c if best is None else min(best, c)
    return best / 1000.0


def decide(p, idx, test_ids, sec_idx, exp_mask, cost):
    """Split candidates into: run (verify in a child), listed (report unrun), maybe_exp
    (expensive tests reported only if a verified cheap candidate fails)."""
    exp = p.run & exp_mask
    keep = 0
    direct = 0
    for i in bits(exp):
        listing = sec_idx.get(i, 0.0) + 2.0
        if (1 << i) & p.must:
            direct |= 1 << i  # reads a changed fixture: report it
        elif est_cost(p, idx, test_ids, 1 << i, cost) < 0.8 * listing:
            keep |= 1 << i  # verifying (sliced when possible) is cheaper than listing
        elif listing <= 10.0:
            direct |= 1 << i  # moderate: report rather than risk a 300 s miss
    p.listed |= direct
    p.run &= ~direct
    exp &= ~direct
    p.maybe_exp = exp & ~keep
    p.run &= ~p.maybe_exp
    if p.run and p.maybe_exp:
        cheap = sum(sec_idx.get(i, 0.0) for i in bits(p.run))
        peak = max(sec_idx.get(i, 0.0) for i in bits(p.maybe_exp))
        p.stop_early = cheap <= peak
    # whole commit: if reporting every candidate unverified costs less than verifying
    # (estimated CPU, with a margin for estimation error), just report them all
    if p.run:
        all_b = p.run | p.maybe_exp | p.listed
        verify = est_cost(p, idx, test_ids, p.run, cost) * 1.15 + (sched_cost(p.listed, sec_idx) if p.listed else 0.0)
        report = sched_cost(all_b, sec_idx)
        if report < verify:
            p.notes.append("list-all %.2f<%.2f" % (report, verify))
            p.listed = all_b
            p.run = 0
            p.maybe_exp = 0
            p.stop_early = False
            return
    if not p.run or p.maybe_exp:
        return
    # per test class: list instead of verifying when verifying costs more CPU than
    # the recorded seconds it would add to the schedule
    groups = {}
    for i in bits(p.run):
        groups.setdefault(test_ids[i].rsplit("::", 1)[0], []).append(i)
    for cls, lst in groups.items():
        b = 0
        for i in lst:
            b |= 1 << i
        run_c = est_cost(p, idx, test_ids, b, cost) - CHILD_BASE
        list_c = sum(sec_idx.get(i, 0.0) for i in lst) + 1.0
        if run_c > list_c:
            p.listed |= b
            p.run &= ~b
            p.notes.append("list %s %.2f>%.2f" % (cls.rsplit("::", 1)[1], run_c, list_c))


def run_children(todo, test_ids, sec_idx, results):
    if os.name == "nt":
        # local development: run sequentially in-process copies via subprocess
        import subprocess

        import tempfile

        tmp = tempfile.mkdtemp(prefix="br_")
        os.environ["TMPDIR"] = tmp
        for p in todo:
            inp = os.path.join(tmp, p.cid + ".in")
            outp = os.path.join(tmp, p.cid + ".out")
            with open(inp, "wb") as fh:
                pickle.dump((p, test_ids, sec_idx), fh)
            r = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--child", inp, outp],
                capture_output=True,
            )
            err = r.stderr.decode("utf-8", "replace")
            if "Error" in err or "crash" in err:
                sys.stderr.write(err[-1500:])
            try:
                with open(outp, "rb") as fh:
                    results[p.cid] = unpack(pickle.load(fh), p)
            except Exception:
                results[p.cid] = set(bits(p.run))
        return

    preload()
    if any(exec_ok(p) and any(test_ids[i].startswith(exec_cache.CLASS + "::") for i in bits(p.run)) for p in todo):
        load_exec_rows()  # once in the parent; forked children share it
    ru = resource.getrusage(resource.RUSAGE_SELF)
    log("parent cpu after preload %.3f" % (ru.ru_utime + ru.ru_stime))
    par = int(os.environ.get("BR_PAR", "2"))
    running = {}
    queue = list(todo)
    budget = float(os.environ.get("BR_BUDGET", "120"))
    while queue or running:
        while queue and len(running) < par:
            p = queue.pop(0)
            if time.time() - T0 > budget:
                log("time budget hit: listing", p.cid, "unverified")
                results[p.cid] = set(bits(p.run)) | set(bits(p.maybe_exp))
                continue
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                with os.fdopen(w, "wb") as fw:
                    child_run(p, test_ids, sec_idx, fw)
                os._exit(0)
            os.close(w)
            running[pid] = (p, os.fdopen(r, "rb"))
        if not running:
            continue
        pid, status, ru = os.wait4(-1, 0)
        if pid in running:
            p, fr = running.pop(pid)
            log("cpu %s %.3f" % (p.cid, ru.ru_utime + ru.ru_stime))
            data = fr.read()
            fr.close()
            try:
                results[p.cid] = unpack(pickle.loads(data), p)
            except Exception:
                log("child failed", p.cid, status)
                results[p.cid] = set(bits(p.run))


def preload():
    for m in (
        "unittest", "unittest.mock", "json", "csv", "gzip", "datetime", "decimal", "re",
        "typing", "itertools", "collections", "concurrent.futures", "multiprocessing",
        "logging", "textwrap", "functools", "copy", "math", "pickle", "tracemalloc",
        "duckdb", "numpy", "pandas", "pandas.testing", "dateutil", "pytz", "zoneinfo",
        "sqlite3", "pathlib", "pkgutil", "importlib", "inspect", "ast", "dataclasses",
        "enum", "string", "random", "subprocess",
    ):
        try:
            __import__(m)
        except Exception:
            pass


def measure():
    """Offline helper (CI only): CPU seconds to run each test alone in a forked child,
    and to import each test module alone. Prints one JSON object to stdout."""
    with open(INDEX, "rb") as fh:
        idx = pickle.load(fh)
    test_ids = idx["tests"]
    only = os.environ.get("BR_MEASURE_ONLY")
    preload()
    out = {"test": {}, "import": {}}

    def one(run_bits, import_only):
        p = Plan("m%d" % os.getpid())
        p.run = run_bits
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            if import_only:
                os.environ["BR_IMPORT_ONLY"] = "1"
            with os.fdopen(w, "wb") as fw:
                child_run(p, test_ids, {}, fw)
            os._exit(0)
        os.close(w)
        with os.fdopen(r, "rb") as fr:
            data = fr.read()
        _, status, ru = os.wait4(pid, 0)
        try:
            failed = bool(pickle.loads(data)["failing"])
        except Exception:
            failed = True
        return ru.ru_utime + ru.ru_stime, failed

    files = {}
    for i, tid in enumerate(test_ids):
        if only and only not in tid:
            continue
        files.setdefault(tid.split("::")[0], []).append(i)
    for f, lst in files.items():
        c, _ = one(1 << lst[0], True)
        out["import"][f] = round(c, 4)
        for i in lst:
            c, failed = one(1 << i, False)
            out["test"][test_ids[i]] = [round(c, 4), failed]
        log("measured", f, out["import"][f])
    c, _ = one(0, True)
    out["base"] = round(c, 4)
    sys.stdout.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--measure":
        measure()
    elif len(sys.argv) > 1 and sys.argv[1] == "--child":
        with open(sys.argv[2], "rb") as fh:
            p, test_ids, sec_idx = pickle.load(fh)
        with open(sys.argv[3], "wb") as fw:
            child_run(p, test_ids, sec_idx, fw)
    else:
        main()
