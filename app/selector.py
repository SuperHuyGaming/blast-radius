"""Blast Radius selector.

For each commit: find the tests that can be affected by the patch (maps built from the pinned
commit), apply the patch in a forked child process, run those tests, and select the tests
that fail. Logs go to stderr; only the answers go to stdout.
"""
from __future__ import annotations

import collections
import gc
import json
import marshal
import os
import re
import zlib
import select
import signal
import sys
import time
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine  # noqa: E402
import patchrules  # noqa: E402

ROOT = os.environ.get("BR_ROOT", "/repo")
DATA = os.environ.get("BR_DATA", "/data")
MAPS = os.environ.get("BR_MAPS", os.path.join(HERE, "maps"))
DEBUG = os.environ.get("BR_DEBUG")
RUN_ALL = os.environ.get("BR_RUN_ALL")  # validation mode: run every candidate, no early stop
RUN_SLOW = os.environ.get("BR_RUN_SLOW")  # validation mode: also run the slow tests
NO_REDUCE = os.environ.get("BR_NO_REDUCE")  # validation mode: always run whole tests
MAPS_OBJ = None
ANSWER_FD = None
BASE_STATE = None
BASE_FUNCTION_KEYS = frozenset()
REGISTRY_CLEAR_AT = None  # first test after tests/test_dialect_entry_points.py (see registry_clear_at)
NO_DIFF = os.environ.get("BR_NO_DIFF")  # validation: no import-state diff
CLASS_FILTER = os.environ.get("BR_CLASS_FILTER") == "1"  # off: the instantiation trace does not see every reader
FRESH_REDUCE = os.environ.get("BR_FRESH_REDUCE", "1") == "1"

WORKER_COST = 2.0
HOLD_SECONDS = float(os.environ.get("BR_HOLD", "1.5"))  # test runs below this CPU cost always start
COMMIT_SECONDS = float(os.environ.get("BR_COMMIT", "8.0"))  # CPU limit of the test runs of one commit
FRESH_SECONDS = float(os.environ.get("BR_FRESH", "3.0"))  # the same for a commit that needs a new import
INERT_SECONDS = 1.0  # the same when the import state did not change at all
HOLD_FACTOR = float(os.environ.get("BR_HOLD_FACTOR", "0.7"))
QUIET_HOLD_SECONDS = float(os.environ.get("BR_QUIET_HOLD", "2.0"))
UNSEEN_CLASS = 10  # more tests of one class without a verdict than this: the class is not verified
UNSEEN_HOLD_SECONDS = 5.0  # in a class that is not verified, held tests up to this length are selected
AFTER_FAIL_RATIO = float(os.environ.get("BR_AFTER_FAIL", "0.5"))
MANY_FAILURES = 8  # with this many failing tests, a long test on the same code is selected too
TEST_TIMEOUT = 4.0  # a test that runs longer than this (and 25 times its usual time) counts as hung
SETUP_TIMEOUT = 6.0  # a child whose imports take longer than this is stopped
SLOWDOWN = float(os.environ.get("BR_SLOWDOWN", "1.0"))  # local runs on a loaded machine
WALL_LIMIT = float(os.environ.get("BR_WALL", "150"))  # the batch must end well inside 3 minutes


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Maps


class Maps:
    def __init__(self, path):
        self.path = path
        with open(os.path.join(path, "meta.bin"), "rb") as f:
            self.meta = marshal.load(f)
        with open(os.path.join(path, "sets.idx"), "rb") as f:
            self.offsets = array("I")
            self.offsets.frombytes(f.read())
        self.sets_file = open(os.path.join(path, "sets.bin"), "rb")
        self.file_cache = {}
        self.set_cache = {}
        self.unit_cache = {}
        self.test_cache = {}
        self._paths = None
        self._readers = None
        self._class_users = None
        try:
            with open(os.path.join(path, "cost.bin"), "rb") as f:
                self.cost = marshal.load(f)
        except OSError:
            self.cost = None

    def file(self, rel):
        rec = self.file_cache.get(rel)
        if rec is None:
            fid = self.meta["files"].get(rel)
            if fid is None:
                return None
            with open(os.path.join(self.path, "f", f"{fid}.bin"), "rb") as f:
                rec = self.file_cache[rel] = marshal.loads(zlib.decompress(f.read()))
        return rec

    def unit_entries(self, rel, line):
        """{test: pattern id} for the tests whose parts are known to run this line."""
        fid = self.meta["files"].get(rel)
        if fid is None:
            return {}
        key = (fid, line // 256)
        chunk = self.unit_cache.get(key)
        if chunk is None:
            try:
                with open(os.path.join(self.path, "u", "%d_%d.bin" % key), "rb") as f:
                    chunk = marshal.loads(zlib.decompress(f.read()))
            except OSError:
                chunk = {}
            self.unit_cache[key] = chunk
        raw = chunk.get(line)
        if raw is None:
            return {}
        if isinstance(raw, bytes):
            arr = array("H")
            arr.frombytes(raw)
            raw = chunk[line] = dict(zip(arr[0::2], arr[1::2]))
        return raw

    def test_units(self, number):
        rec = self.test_cache.get(number, 0)
        if rec == 0:
            try:
                with open(os.path.join(self.path, "t", "%d.bin" % number), "rb") as f:
                    rec = marshal.loads(zlib.decompress(f.read()))
            except OSError:
                rec = None
            self.test_cache[number] = rec
        return rec

    def readers(self, name):
        """The function lines that read 'name': list of (path, line)."""
        if self._readers is None:
            try:
                with open(os.path.join(self.path, "readers.bin"), "rb") as f:
                    self._readers = marshal.loads(zlib.decompress(f.read()))
            except OSError:
                self._readers = {}
        raw = self._readers.get(name)
        if not raw:
            return ()
        arr = array("I")
        arr.frombytes(raw)
        paths = self.paths()
        return [(paths[arr[i]], arr[i + 1]) for i in range(0, len(arr), 2)]

    def class_users(self, name):
        """Ids of the classes whose instances can read an attribute defined by class 'name',
        or None when that cannot be told from instantiations."""
        if self._class_users is None:
            try:
                with open(os.path.join(self.path, "classes.bin"), "rb") as f:
                    self._class_users = marshal.loads(zlib.decompress(f.read()))
            except OSError:
                self._class_users = {}
        return self._class_users.get(name)

    def paths(self):
        if self._paths is None:
            self._paths = {fid: rel for rel, fid in self.meta["files"].items()}
        return self._paths

    def tests(self, set_id):
        if set_id == 0:
            return ()
        s = self.set_cache.get(set_id)
        if s is None:
            a, b = self.offsets[set_id], self.offsets[set_id + 1]
            self.sets_file.seek(a)
            arr = array("H")
            arr.frombytes(self.sets_file.read(b - a))
            s = self.set_cache[set_id] = arr
        return s


# ---------------------------------------------------------------------------
# Planning


class Plan:
    __slots__ = ("commit", "mode", "cands", "work", "files", "note", "all_fail", "blind", "lines", "keeps", "broken", "times", "pool_died", "fpairs", "stmt_lines", "diff", "def_units", "data_cands", "skipped", "diffable", "class_filter", "plain_lines", "unaffected", "protect", "enders", "gone", "fixture_users")

    def __init__(self, commit):
        self.commit = commit
        self.mode = "noop"  # noop | hot | fresh | all
        self.cands = set()  # tests that can be affected
        self.work = []  # (abs path, [(old code, new code)]) for in-memory replacement
        self.files = {}  # repo-relative path -> new text (None = deleted)
        self.note = []
        self.all_fail = False
        self.blind = set()  # tests selected without a run when the commit is not clean
        self.lines = {}  # repo-relative .py path -> lines of the changed statements (call-time code)
        self.keeps = {}  # test -> (statements, fixture entries, pool tasks) to run; absent = whole test
        self.broken = []  # modules that fail to import with the patch
        self.times = {}  # test -> CPU seconds of its run (for logs)
        self.pool_died = []  # tests during which a class-level process pool died
        self.fpairs = []  # (old code, new code) of changed function bodies that also run at import
        self.stmt_lines = {}  # path -> lines of changed function statements (import-time commits)
        self.diff = None  # names whose import-time value the patch changes (import-state diff)
        self.def_units = {}  # path -> lines of functions whose header (defaults, decorators) changed
        self.data_cands = set()  # tests that read a changed file as data
        self.skipped = set()  # tests not run because another test had failed already
        self.diffable = True  # every changed .py file is a modified sqlglot/ module
        self.class_filter = None  # class ids: only parts that create one of these can read the change
        self.plain_lines = set()  # (path, line) of changed code itself: no class filter there
        self.unaffected = set()  # candidates that cannot read the changed value at all
        self.protect = set()  # tests whose verdicts decide about long held tests
        self.enders = {}  # tests whose failure can end the runs of the commit (see execute)
        self.fixture_users = set()  # tests that walk a class fixture list of which few entries changed
        self.gone = set()  # module or class names that the patch removes (no reduced runs then)


CODING_RE = re.compile(r"coding[:=]\s*[-\w.]+")


def _blank_or_comment(lines):
    return all((not s.strip()) or s.lstrip().startswith("#") for s in lines)


def _indent(s):
    return len(s) - len(s.lstrip())


def plan_python_file(plan: Plan, fp, maps: Maps, old_text: str):
    """Classifies the edit of one known .py file. Returns True when a fresh import is needed."""
    rel = fp.old_path
    rec = maps.file(rel)
    if rec is None:
        return True
    units, flags, inames, stmt_set, stmt_pex, unit_set, unit_pex, importers, file_set, span_a, span_b = rec[:11]
    touched = plan.lines.setdefault(rel, set())
    old_lines = old_text.split("\n")
    new_lines = fp.new_text.split("\n")
    full = os.path.join(ROOT, rel)
    hit_units = {}
    for g in fp.groups:
        o, nr, nl, na, removed, added = g
        hit = []
        for ui, u in enumerate(units):
            s, e = u[0], u[1]
            if nr > 0:
                if s <= o + nr - 1 and o <= e:
                    hit.append(ui)
            elif s < o <= e:
                hit.append(ui)
        if not hit:
            if _blank_or_comment(added) and _blank_or_comment(removed):
                if nl > 2 or not any(CODING_RE.search(a) for a in added + removed):
                    continue
            return True
        for ui in hit:
            hit_units.setdefault(ui, []).append(g)
    for ui, groups in hit_units.items():
        s, e, class_path, kind = units[ui]
        if kind == 1:
            return True
        delta_before = 0
        delta_in = 0
        for g in fp.groups:
            if g[0] + g[1] - 1 < s and g[0] < s:
                delta_before += g[3] - g[1]
            elif g[0] <= e:
                delta_in += g[3] - g[1]
        try:
            a = engine.compile_unit(old_lines, s, e, class_path, full, flags, inames)
            b = engine.compile_unit(
                new_lines, s + delta_before, e + delta_before + delta_in, class_path, full, flags, inames
            )
        except (SyntaxError, ValueError):
            return True
        if a is None or b is None:
            return True
        pairs = []
        if not engine.deftime_equal(a, b, pairs):
            pairs = []
            if patchrules.tests_read_docs(maps) or not patchrules.equal_but_doc(a, b, pairs):
                return True
            plan.note.append("docstring only")
        if all(engine.func_code_equal(x, y) for x, y in pairs):
            continue  # comments or layout only
        if not class_path and any(line.startswith("def __getattr__(") for line in old_lines[s - 1:e]):
            # A module __getattr__ answers the lookups of names that the module does not hold
            # yet (lazy imports), and the result is stored. So the pinned run shows only the
            # first lookup of each name, but a changed __getattr__ can fail every lookup:
            # every test that loads the module is a candidate, and no reduced run applies.
            plan.cands.update(maps.tests(importers))
            plan.gone.add("__getattr__")
            plan.note.append("module __getattr__")
        simple = True
        for g in groups:
            o, nr, nl, na, removed, added = g
            if nr != na or any(_indent(x) != _indent(y) for x, y in zip(removed, added)):
                simple = False
        if simple:
            for g in groups:
                for ln in range(g[0], g[0] + g[1]):
                    if stmt_pex[ln]:
                        # the function also runs at import: its body change is known here
                        plan.fpairs.extend(pairs)
                        sl = plan.stmt_lines.setdefault(rel, set())
                        for g2 in groups:
                            for l2 in range(g2[0], g2[0] + g2[1]):
                                sl.update(range(span_a[l2], span_b[l2] + 1) if span_a[l2] else (l2,))
                        return True
                    plan.cands.update(maps.tests(stmt_set[ln]))
                    touched.update(range(span_a[ln], span_b[ln] + 1) if span_a[ln] else (ln,))
        else:
            if unit_pex[ui]:
                return True
            plan.cands.update(maps.tests(unit_set[ui]))
            touched.update(range(s, e + 1))
        plan.work.append((full, pairs))
    return False


def add_import_time_candidates(plan: Plan, fp, rec, maps: Maps):
    """Candidates for an edit that changes what the module does at import. The map gives,
    for a line of module-level or class-level code, the tests that use what the line defines.
    A line without an entry falls back to every test that loads or runs the file."""
    stmt_set, def_set, def_known = rec[3], rec[11], rec[12]
    readers = rec[13] if len(rec) > 13 else {}
    paths = maps.paths()
    n = len(def_set) - 2
    complete = True
    reader_lines = {}
    units = rec[0]
    # a change in a function header (defaults, decorators) reaches the tests that run the function
    for o, nr, nl, na, removed, added in fp.groups:
        for s, e, _, kind in units:
            if kind == 0 and s <= o <= e and e > s:
                plan.def_units.setdefault(fp.old_path, set()).update(range(s, e + 1))
    for o, nr, nl, na, removed, added in fp.groups:
        lines = range(o, o + nr) if nr else (o - 1, o)
        simple = nr == na and all(_indent(x) == _indent(y) for x, y in zip(removed, added))
        if not simple and not (_blank_or_comment(added) and _blank_or_comment(removed)):
            complete = False
        for ln in lines:
            if not 0 < ln <= n:
                continue
            if def_known[ln]:
                plan.cands.update(maps.tests(def_set[ln]))
                rs = readers.get(ln) if simple else None
                if rs is None:
                    reader_lines = None
                elif reader_lines is not None:
                    for rfid, rline in rs:
                        reader_lines.setdefault(paths[rfid], set()).add(rline)
            elif stmt_set[ln]:
                reader_lines = None
                plan.cands.update(maps.tests(stmt_set[ln]))
            elif nr:
                complete = False
    if not complete:
        plan.cands.update(maps.tests(rec[7]))
        plan.cands.update(maps.tests(rec[8]))
        plan.note.append("import-time fallback")
        reader_lines = None
    if reader_lines:
        # a class constant that only functions read: a test part is affected only when it
        # runs one of the reading lines, so the reduced runs apply
        for p, lines in reader_lines.items():
            plan.lines.setdefault(p, set()).update(lines)
        plan.note.append("reader lines")
    else:
        plan.lines["?"] = None  # no reduced runs for this commit


def make_plan(commit, text, maps: Maps) -> Plan:
    plan = Plan(commit)
    fps = engine.parse_patch(text)
    if not fps:
        plan.note.append("no files in patch")
        return plan
    fresh = False
    for fp in fps:
        path = fp.new_path if fp.status != "delete" else fp.old_path
        old_text = None
        if fp.status in ("modify", "delete", "rename"):
            try:
                with open(os.path.join(ROOT, fp.old_path), encoding="utf-8", newline="") as f:
                    old_text = f.read()
            except (OSError, UnicodeDecodeError):
                fp.ok = False
        if fp.ok:
            engine.apply_file_patch(fp, old_text)
        if not fp.ok:
            plan.note.append(f"cannot apply {path}")
            plan.mode = "all"
            return plan
        if fp.status == "rename":
            plan.files[fp.old_path] = None
        plan.files[path] = fp.new_text
        is_py = path.endswith(".py")
        in_tree = path.startswith(("sqlglot/", "tests/"))
        scope = patchrules.pytest_scope(maps, fp, path, old_text)
        if scope is not None:
            # pytest itself reads this file (conftest.py, its options); the direct runs do not
            plan.blind.update(scope)
            plan.note.append("pytest file %s: %d tests" % (path, len(scope)))
            continue
        if fp.status in ("add", "rename") and patchrules.shadows_module(path):
            plan.blind.update(range(len(maps.meta["test_ids"])))
            plan.note.append("shadows a module")
        if is_py and in_tree:
            refs = None
            if fp.status == "add":
                # a new module is loaded only by its name: the tests that can reach that name
                base = os.path.basename(path)[:-3]
                word = os.path.basename(os.path.dirname(path)) if base == "__init__" else base
                refs = patchrules.referencing_tests(maps, word)
                if refs is not None:
                    plan.cands.update(refs)
                    plan.note.append("new module, %d referencing tests" % len(refs))
                    if refs:
                        fresh = True
                        plan.lines["?"] = None
            if fp.status != "modify" and refs is None:
                fresh = True
                plan.lines["?"] = None
                rec = maps.file(fp.old_path) if fp.old_path else None
                if rec is not None:
                    plan.cands.update(maps.tests(rec[7]))
                    plan.cands.update(maps.tests(rec[8]))
            elif fp.status == "modify" and plan_python_file(plan, fp, maps, old_text):
                fresh = True
                plan.lines.pop(fp.old_path, None)
                rec = maps.file(fp.old_path)
                if rec is not None:
                    add_import_time_candidates(plan, fp, rec, maps)
                # a name that the patch removes or renames fails every line that uses it
                gone, extra = patchrules.gone_name_tests(maps, fp, old_text)
                if gone:
                    plan.gone.update(gone)
                    plan.cands.update(extra)
                    plan.lines["?"] = None
                    plan.note.append("names gone %s: %d tests" % (sorted(gone)[:3], len(extra)))
            if os.path.basename(path) == "__init__.py" and path.startswith("tests/"):
                # pytest imports the package of every test file below it; the coverage maps do
                # not list the package __init__ files as imported
                plan.cands.update(patchrules.tests_under(maps, os.path.dirname(path)))
                plan.lines["?"] = None
        # every test that reads the file as data can change
        for key in {fp.old_path, path}:
            if key:
                sid = maps.meta["opened"].get(key)
                if sid:
                    plan.cands.update(maps.tests(sid))
                    plan.data_cands.update(maps.tests(sid))
        if fp.status != "modify":
            d = os.path.dirname(path)
            sid = maps.meta["listed"].get(d)
            if sid:
                # The 'listed' map also holds the listings that the import system makes for
                # itself. Only tests whose file calls a listing function can see the file set.
                listers = patchrules.listing_tests(maps)
                for n in maps.tests(sid):
                    if listers is None or n in listers:
                        # os.listdir shows the patch in the child (install_listing_overlay);
                        # other ways of listing do not: those tests are selected unrun
                        rel = maps.meta["test_ids"][n].split("::")[0]
                        scans = listers is not None and patchrules.SCAN_RE.search(patchrules.source(rel))
                        (plan.blind if scans else plan.cands).add(n)
                        plan.data_cands.add(n)
        if is_py and not (path.startswith("sqlglot/") and fp.status == "modify"):
            plan.diffable = False
    add_registry_candidates(plan, fps, maps)
    if fresh:
        plan.mode = "fresh"
    elif plan.work or plan.cands - plan.blind:
        plan.mode = "hot"
    elif plan.blind:
        plan.mode = "blind"
    return plan


def registry_clear_at(test_ids):
    """The first test after tests/test_dialect_entry_points.py. Its setUp and tearDown clear
    Dialect._classes, so in the full run every later test finds an empty dialect registry and
    registers each dialect again through _Dialect._try_load (dialect.py, the dir(module) scan)."""
    last = None
    for i, tid in enumerate(test_ids):
        if tid.startswith("tests/test_dialect_entry_points.py::"):
            last = i
    return None if last is None else last + 1


REGISTRY_METHODS = ("classes", "_try_load", "__getitem__", "get", "get_or_raise")


def add_registry_candidates(plan: Plan, fps, maps: Maps):
    """An edit of the registry lookup code reaches, through the registry state, every test
    after the clear that looks a dialect up by name, not only the tests that ran the line."""
    import ast

    clear_at = registry_clear_at(maps.meta["test_ids"])
    if clear_at is None:
        return
    for fp in fps:
        if fp.old_path != "sqlglot/dialects/dialect.py" or fp.status != "modify":
            continue
        try:
            with open(os.path.join(ROOT, fp.old_path), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError):
            return
        spans = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name in REGISTRY_METHODS:
                        start = min([item.lineno] + [d.lineno for d in item.decorator_list])
                        spans.append((start, item.end_lineno))
        hit = False
        for o, nr, nl, na, removed, added in fp.groups:
            a, b = o, o + max(nr, 1) - 1
            if any(s <= b and a <= e for s, e in spans):
                hit = True
        if not hit:
            continue
        rec = maps.file(fp.old_path)
        if rec is None:
            return
        extra = set()
        for s, e in spans:
            for ln in range(s, e + 1):
                if ln < len(rec[3]):
                    extra.update(n for n in maps.tests(rec[3][ln]) if n >= clear_at)
        plan.cands.update(extra)
        plan.note.append("registry: %d tests after the clear" % len(extra))


BASE_PATTERN = 0xFFFF
SCALED_IDS = frozenset((
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
    "tests/test_optimizer.py::TestOptimizer::test_scope",
))


def compute_keeps(plan: Plan, maps: Maps, tests):
    """For each candidate test whose independent parts are mapped: the parts that run the
    changed code or read a changed fixture entry. Other parts cannot change their result."""
    keeps = {}
    if plan.mode not in ("hot", "fresh", "fresh_purge"):
        return keeps
    if any(p.startswith("tests/") and p.endswith(".py") for p in plan.files):
        return keeps
    for n in plan.cands:
        if tests[n]["id"] in SCALED_IDS:
            keeps[n] = (None, None, None, True, None)
    if plan.mode != "hot" and (None in plan.lines.values() or not plan.lines):
        return keeps
    code_lines = [(rel, ln) for rel, lines in plan.lines.items() for ln in lines]
    fixtures = {}
    for p in plan.files:
        if p.startswith("tests/fixtures/"):
            sid = maps.meta["opened"].get(p)
            fixtures[p[len("tests/fixtures/"):]] = set(maps.tests(sid)) if sid else set()
    for n in plan.cands:
        trec = maps.test_units(n)
        if trec is None:
            continue
        stmts, units, patterns = trec[2], trec[3], trec[4]
        unit_classes = trec[8] if len(trec) > 8 else None
        base_classes = trec[9] if len(trec) > 9 else None
        cfilter = plan.class_filter if unit_classes is not None else None
        chosen = set()
        if code_lines:
            seen = False
            whole = False
            for rel, ln in code_lines:
                pat = maps.unit_entries(rel, ln).get(n)
                if pat is None:
                    continue
                seen = True
                plain = cfilter is None or (rel, ln) in plan.plain_lines
                if pat == BASE_PATTERN:
                    # the always-run part runs the line: the whole test runs, unless that
                    # part creates no object that can read the changed value
                    if plain or base_classes is None or not cfilter.isdisjoint(base_classes):
                        whole = True
                        break
                    continue
                if plain:
                    chosen.update(patterns[pat])
                else:
                    for u in patterns[pat]:
                        uc = unit_classes[u]
                        if uc is None or not cfilter.isdisjoint(uc):
                            chosen.add(u)
            if whole:
                continue
            if not seen and (plan.work or plan.mode != "hot"):
                continue  # the coverage maps disagree: run the whole test
        keep_stmts = set()
        keep_files = {}
        keep_tasks = set() if any(u[0] == "t" for u in units) else None
        for ui in chosen:
            kind, name, i, parent = units[ui]
            if kind == "s":
                keep_stmts.add(i)
            elif kind == "f":
                keep_files.setdefault(name, set()).add(i)
            else:
                keep_tasks.add(i)
            if parent >= 0:
                keep_stmts.add(parent)
        ok = True
        for name, readers in fixtures.items():
            if n in readers:
                parents = {u[3] for u in units if u[0] == "f" and u[1] == name}
                if not parents:
                    ok = False  # the test reads the file in a way that the maps do not know
                    break
                keep_stmts.update(p for p in parents if p >= 0)
        if not ok:
            continue
        if code_lines and not chosen and not keep_stmts and not fixtures and plan.class_filter is not None:
            # no part of this test creates an object that can read the changed value
            plan.unaffected.add(n)
            continue
        keeps[n] = (keep_stmts, keep_files, keep_tasks, n in keeps, chosen)
    return keeps


def apply_keeps(plan: Plan, maps: Maps, tests):
    """Child side: replaces the code of the reduced test methods. Returns {test: (files, tasks)}."""
    import importlib

    import reduce

    out = {}
    lines_cache = {}
    for n, (keep_stmts, keep_files, keep_tasks, scaled, _) in plan.keeps.items():
        test_id = tests[n]["id"]
        rel, cls_name, meth = test_id.split("::")
        trec = maps.test_units(n)
        shape = None
        if trec is not None:
            shape = (trec[0], trec[1], trec[2])
            if keep_stmts is not None and {i for i, st in enumerate(trec[2]) if st[2]} <= keep_stmts:
                keep_stmts = None  # every statement stays
        if keep_stmts is not None or scaled:
            frec = maps.file(rel)
            if frec is None:
                continue
            if shape is None:
                shape = maps.meta["shapes"].get(n)
                if shape is None:
                    continue
            lines = lines_cache.get(rel)
            if lines is None:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    lines = lines_cache[rel] = f.read().split("\n")
            try:
                cls = getattr(importlib.import_module(rel[:-3].replace("/", ".")), cls_name)
            except Exception:  # noqa: BLE001
                continue
            if not reduce.apply_reduction(cls, test_id, os.path.join(ROOT, rel), shape, lines,
                                          frec[1], frec[2], keep_stmts, scaled):
                if DEBUG:
                    sys.stderr.write("reduction not applied: %s\n" % test_id)
                continue
        out[n] = (keep_files, keep_tasks)
    return out


# ---------------------------------------------------------------------------
# Children


def child_main(plan: Plan, order, tests, wfd, index, skip_after_fail, plan_fd=None):
    import runner

    last = [time.process_time()]

    import pool as pool_module

    def emit(number, failed):
        now = time.process_time()
        code = 2 if failed is None else (1 if failed else 0)  # 2 = not run
        os.write(wfd, b"%d %d %d\n" % (number, code, int((now - last[0]) * 1e6)))
        last[0] = now
        if pool_module.InlinePool.broke_any:
            # the class's shared process pool died in this test: in a full run every later
            # test of the class that uses the pool fails
            pool_module.InlinePool.broke_any = False
            os.write(wfd, b"P %d\n" % number)

    cleared = [False]

    def started(number):
        if not cleared[0] and REGISTRY_CLEAR_AT is not None and number >= REGISTRY_CLEAR_AT:
            # the full run reaches this test with an empty dialect registry (see registry_clear_at)
            cleared[0] = True
            try:
                sys.modules["sqlglot.dialects.dialect"].Dialect._classes.clear()
            except Exception:  # noqa: BLE001  (sqlglot does not import: the tests report that)
                pass
        os.write(wfd, b"S %d\n" % number)

    try:
        if ANSWER_FD is not None:
            os.close(ANSWER_FD)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        if not DEBUG:
            os.dup2(devnull, 2)
        if DEBUG:
            import faulthandler

            faulthandler.enable(file=sys.stderr)
        if os.environ.get("BR_FAULT"):
            import faulthandler

            faulthandler.dump_traceback_later(float(os.environ["BR_FAULT"]), file=sys.stderr)
        t_child = time.process_time()
        py_files = {p: t for p, t in plan.files.items() if p.endswith(".py")}
        if plan.mode in ("fresh", "fresh_purge"):
            if plan.mode == "fresh_purge":
                for name in [m for m in sys.modules if m.partition(".")[0] in ("sqlglot", "tests")]:
                    del sys.modules[name]
            engine.install_overlay(ROOT, py_files)
            # a module that cannot be imported any more fails every test that loads it
            import preload

            # the patched test modules too: a test package or helper module that raises fails
            # every test file that imports it
            test_mods = [
                p[:-3].replace("/", ".").removesuffix(".__init__") for p in py_files
                if p.startswith("tests/") and os.path.basename(p) != "conftest.py"
            ]
            bad = preload.broken_modules(test_mods)
            for name in bad:
                os.write(wfd, b"B %s\n" % name.encode())

            def import_all_tests():
                # Every test file: one that cannot be imported any more fails all its tests
                # (pytest imports every test file before the first test runs). This comes
                # after the import-state comparison, because test modules change that state.
                names = sorted({t["id"].split("::")[0][:-3].replace("/", ".") for t in tests} - set(test_mods))
                for name in preload.broken_test_modules(names):
                    os.write(wfd, b"B %s\n" % name.encode())

            if plan_fd is None:
                import_all_tests()
            if preload.exec_fresh_ok(plan.files):
                runner.ON_IMPORT[preload.EXEC_KEY[0]] = preload.exec_fresh_hook
            if plan_fd is not None:
                # What did the import leave behind, compared with the pinned tree? The parent
                # turns the changed names into the tests (and test parts) that read them.
                import statefp

                alias = {}
                if plan.fpairs:
                    f0 = statefp.Fingerprinter()
                    stack = list(plan.fpairs)
                    while stack:
                        old, new = stack.pop()
                        alias[f0.code(new)] = f0.code(old)
                        oc = [k for k in old.co_consts if isinstance(k, type(old))]
                        nc = [k for k in new.co_consts if isinstance(k, type(new))]
                        if len(oc) == len(nc):
                            stack.extend(zip(oc, nc))
                new_state, fpr = statefp.snapshot(alias)
                changed = statefp.diff(BASE_STATE, new_state)
                if len(changed) > 300:
                    os.write(wfd, b"D many\n")
                else:
                    rows = [[list(k), k in fpr.function_keys or k in BASE_FUNCTION_KEYS] for k in changed]
                    os.write(wfd, b"D " + json.dumps(rows).encode() + b"\n")
                import_all_tests()
                chunks = []
                while True:
                    chunk = os.read(plan_fd, 1 << 16)
                    if not chunk:
                        break
                    chunks.append(chunk)
                os.close(plan_fd)
                todo = marshal.loads(b"".join(chunks)) if chunks else None
                if not todo or not todo["order"]:
                    os.write(wfd, b"END\n")
                    os._exit(0)
                order = todo["order"]
                skip_after_fail = todo["skip"]
                plan.enders = todo["enders"]
                plan.keeps = todo["keeps"]
            if bad:
                # a test that loads a module that cannot be imported fails (the parent selects
                # it anyway): running it would only repeat the failing import
                dead = patchrules.importer_tests(MAPS_OBJ, bad)
                for n in order:
                    if n in dead:
                        emit(n, True)
                order = [n for n in order if n not in dead]
        else:
            for full, pairs in plan.work:
                swaps = engine.plan_swaps(pairs, index)
                if swaps is None:
                    os.write(wfd, b"REDO\n")
                    os._exit(0)
                for f, code in swaps:
                    f.__code__ = code
        read_old = engine.install_open_overlay(ROOT, plan.files)
        patchrules.install_listing_overlay(ROOT, plan.files)
        if plan.mode == "hot":
            import preload

            # the parent built the shared class fixtures from the pinned files
            for mod_name, failed in preload.refresh_class_fixtures(ROOT, plan.files):
                if failed:  # setUpClass raises in the full run: every test of the class fails
                    os.write(wfd, b"B %s\n" % mod_name.encode())
        import reduce

        keeps = None
        if plan.mode != "hot" and plan.keeps and not NO_REDUCE:
            if any(k[0] is not None or k[1] is not None for k in plan.keeps.values()):
                reduce.install_loaders()
            keeps = apply_keeps(plan, MAPS_OBJ, tests)
        if plan.mode == "hot" and reduce.install_loaders():
            # an unchanged fixture entry that does not run changed code cannot change a result
            changed = {}
            for rel, text in plan.files.items():
                if rel.startswith("tests/fixtures/") and text is not None:
                    changed.update(reduce.changed_entries(rel[len("tests/fixtures/"):], read_old))
            if changed:
                reduce.STATE.keep_changed = changed
                reduce.STATE.changed_only = not plan.work
                if not plan.work:
                    import preload

                    preload.reduce_class_fixtures(changed)
            if plan.keeps and not NO_REDUCE:
                keeps = apply_keeps(plan, MAPS_OBJ, tests)
        t_setup = time.process_time()
        runner.run_tests([(n, tests[n]["id"]) for n in order], emit, False, keeps, started, skip_after_fail, plan.enders)
        if DEBUG:
            sys.stderr.write("child %s setup %.3f run %.3f\n" % (plan.commit, t_setup - t_child, time.process_time() - t_setup))
        os.write(wfd, b"END\n")
    except BaseException as e:  # noqa: BLE001
        try:
            os.write(wfd, ("ERR %r\n" % (e,)).encode()[:400])
        except OSError:
            pass
    finally:
        os._exit(0)


def run_child(plan, order, tests, index, allowed, time_left, skip_after_fail=(), on_diff=None, cap=None):
    """Runs the tests in a forked child. Returns (verdicts, status, hung test or None).

    status: END, REDO, ERR, TIMEOUT, DIED, OVER. allowed[n] = seconds that test n may take.
    cap: seconds that all the tests together may take from the start of the first one.
    on_diff: for a commit that changes import-time code. The child first reports which
    import-time values differ from the pinned tree; on_diff(names) then decides what to run."""
    r, w = os.pipe()
    plan_r = plan_w = None
    if on_diff is not None:
        plan_r, plan_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        if plan_w is not None:
            os.close(plan_w)
        try:
            os.setpgid(0, 0)
        except OSError:
            pass
        child_main(plan, order, tests, w, index, skip_after_fail, plan_r)
        os._exit(0)
    os.close(w)
    if plan_r is not None:
        os.close(plan_r)
    verdicts = {}
    status = "DIED"
    hung = None
    broken = plan.broken
    buf = b""
    t_end = time.monotonic() + time_left
    current = None
    t_test = time.monotonic() + SETUP_TIMEOUT * SLOWDOWN  # time for the child's own setup (imports)
    t_cap = None
    try:
        while True:
            now = time.monotonic()
            if now >= t_end:
                status = "TIMEOUT"
                break
            if t_cap is not None and now >= t_cap:
                status = "OVER"  # the runs cost far more than planned: the rest is selected unrun
                break
            if now >= t_test:
                status = "TIMEOUT"
                hung = current
                break
            ready, _, _ = select.select([r], [], [], max(0.01, min(t_end, t_test, t_cap or t_end) - now))
            if not ready:
                continue
            chunk = os.read(r, 65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                if line in (b"END", b"REDO"):
                    status = line.decode()
                elif line.startswith(b"ERR"):
                    status = "ERR"
                    log(plan.commit, line.decode(errors="replace"))
                elif line.startswith(b"S "):
                    current = int(line[2:])
                    if t_cap is None and cap is not None:
                        t_cap = time.monotonic() + cap
                    t_test = time.monotonic() + allowed.get(current, 30.0)
                elif line.startswith(b"B "):
                    broken.append(line[2:].decode())
                elif line.startswith(b"P "):
                    plan.pool_died.append(int(line[2:]))
                elif line.startswith(b"D "):
                    keys = None if line == b"D many" else json.loads(line[2:])
                    todo = on_diff(keys)
                    allowed = todo.pop("allowed")
                    cap = todo.pop("cap")
                    data = marshal.dumps(todo)
                    while data:
                        data = data[os.write(plan_w, data):]
                    os.close(plan_w)
                    plan_w = None
                    t_test = time.monotonic() + SETUP_TIMEOUT * SLOWDOWN
                else:
                    parts = line.split()
                    if parts[1] == b"2":
                        plan.skipped.add(int(parts[0]))
                    else:
                        verdicts[int(parts[0])] = parts[1] == b"1"
                    if len(parts) > 2:
                        plan.times[int(parts[0])] = int(parts[2]) / 1e6
                    current = None
                    t_test = time.monotonic() + 30.0
    finally:
        # also when this process fails while it talks to the child: the child must not run on
        os.close(r)
        if plan_w is not None:
            os.close(plan_w)
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _, wstatus = os.waitpid(pid, 0)
    if status == "DIED":
        log(plan.commit, "child died, wait status", wstatus, "after", len(verdicts), "tests")
        if current is not None:
            hung = current  # the test that killed the process fails in a full run too
    return verdicts, status, hung


def refine_with_diff(plan: Plan, keys, maps: Maps, tests):
    """Uses the import-state diff of a commit that changes import-time code.

    keys: the module globals and class attributes whose value differs from the pinned tree
    (None = too many). Nothing differs and no function body changed: the Python edits do
    nothing. Otherwise a test part can see the change only when it runs a line that reads
    one of the changed names (or a changed function): the reduced runs apply to those lines."""
    if keys is None:
        plan.note.append("import state: many changes")
        plan.lines = {"?": None}
        return
    names = set()
    whole = bool(plan.gone)  # a removed name can fail any line that mentions it
    owners = set()
    for key, is_function in keys:
        owners.add("%s.%s" % (key[1], key[2]) if key[0] == "A" else None)
        name = key[-1]
        if name == "__bases__" or not isinstance(name, str):
            whole = True
        elif is_function:
            # A function is also reached by computed names (dispatch tables). The reduced
            # runs are safe for it only when the patch changed that function's own header:
            # then every user runs its body, and the body lines are known.
            if not plan.def_units:
                whole = True
        else:
            names.add(name)
    plan.diff = sorted(names)
    if not keys and not plan.fpairs:
        # Nothing that the import leaves behind differs. The edit very likely does nothing;
        # the cheapest candidates still run as a check, and nothing is selected unrun.
        plan.note.append("import state unchanged")
        plan.lines = {"?": None}
        plan.keeps = compute_keeps(plan, maps, tests)
        return
    lines = {}
    for name in names:
        rs = maps.readers(name)
        if not rs or len(rs) > 600:
            whole = True  # read in an unknown way, or read everywhere
            break
        for path, ln in rs:
            lines.setdefault(path, set()).add(ln)
    if whole or not FRESH_REDUCE:
        plan.note.append("import state: no reduced runs")
        plan.lines = {"?": None}
        plan.keeps = compute_keeps(plan, maps, tests)
        return
    # a reader line stands for its whole statement, as for edits in function bodies
    full = {}
    for path, lns in lines.items():
        rec = maps.file(path)
        acc = full.setdefault(path, set())
        for ln in lns:
            if rec is not None and ln < len(rec[9]) and rec[9][ln]:
                acc.update(range(rec[9][ln], rec[10][ln] + 1))
            else:
                acc.add(ln)
    for src in (plan.stmt_lines, plan.def_units):
        for path, lns in src.items():
            full.setdefault(path, set()).update(lns)
            plan.plain_lines.update((path, ln) for ln in lns)
    # Every changed value is an attribute of a known class: a part that creates no object of
    # such a class (or of a dialect that hands the class out) cannot read the new value.
    if owners and None not in owners and CLASS_FILTER:
        wanted = set()
        for owner in owners:
            users = maps.class_users(owner)
            if users is None:
                wanted = None
                break
            wanted.update(users)
        plan.class_filter = wanted
    plan.lines = full
    plan.note.append("import state: %d names" % len(names))
    plan.keeps = compute_keeps(plan, maps, tests)
    if plan.unaffected:
        plan.cands -= plan.unaffected
        plan.note.append("unaffected %d" % len(plan.unaffected))


def estimate_cost(n, plan: Plan, maps: Maps, seconds):
    """Expected CPU seconds of running test n for this commit (whole or reduced)."""
    whole = maps.cost[n] if maps.cost else seconds[n] * 1.5
    if n in plan.fixture_users:
        # the test walks a list of fixture entries, and the run keeps only the changed
        # entries (preload.reduce_class_fixtures): a typo changes one entry, or a few
        return min(whole, 0.05 + 0.05 * whole)
    keep = plan.keeps.get(n)
    if keep is None:
        return whole
    scaled, chosen = keep[3], keep[4]
    if chosen is None:
        return min(whole, 0.15) if scaled else whole
    trec = maps.test_units(n)
    traced_whole = trec[5] or 0.0
    if traced_whole <= 0:
        return whole
    base = trec[6]
    part = base + sum(max(0.0, trec[7][u] - base) for u in chosen)
    cost = whole * min(1.0, part / traced_whole)
    return min(cost, 0.15) if scaled else cost


# ---------------------------------------------------------------------------
# Packing


def pack(numbers, seconds):
    """Splits tests over 1..8 workers to minimise (largest load + 2 s per worker)."""
    if not numbers:
        return []
    items = sorted(numbers, key=lambda n: -seconds[n])
    best = None
    for k in range(1, min(8, len(items)) + 1):
        loads = [0.0] * k
        bins = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            loads[i] += seconds[n]
            bins[i].append(n)
        cost = max(loads) + WORKER_COST * k
        if best is None or cost < best[0] - 1e-9:
            best = (cost, bins)
    return best[1]


# ---------------------------------------------------------------------------


def run_order(cands, tests):
    """The order of tests.json, which is the order of a full pytest run: some tests
    leave global state behind (for instance the dialect registry), so order matters."""
    return sorted(cands)


def main():
    t_start = time.monotonic()
    deadline = t_start + WALL_LIMIT
    # Only the answers may reach stdout. Everything else that writes to fd 1 goes to stderr.
    global ANSWER_FD
    answer_fd = ANSWER_FD = os.dup(1)
    os.dup2(2, 1)
    state = {"answers": {}, "commits": [], "tests": None}
    try:
        run(t_start, deadline, state)
    except BaseException as e:  # noqa: BLE001
        log("selector failed:", repr(e))
        import traceback

        traceback.print_exc(file=sys.stderr)
    # every commit gets a line; a commit without an answer gets every test (never a miss)
    tests = state["tests"]
    commits = state["commits"]
    if tests is None:
        with open(os.path.join(DATA, "tests.json")) as f:
            tests = json.load(f)["tests"]
    if not commits:
        commits = sorted(p[:-6] for p in os.listdir(os.path.join(DATA, "commits")) if p.endswith(".patch"))
    seconds = [t["seconds"] for t in tests]
    everything = None
    out = []
    for c in commits:
        workers = state["answers"].get(c)
        if workers is None:
            if everything is None:
                everything = pack(list(range(len(tests))), seconds)
            workers = everything
        out.append(json.dumps({"commit": c, "workers": workers}))
    data = ("\n".join(out) + "\n").encode() if out else b""
    while data:
        n = os.write(answer_fd, data)
        data = data[n:]
    cg = ""
    try:
        with open("/sys/fs/cgroup/cpu.stat") as f:
            cg = f.readline().strip()
    except OSError:
        pass
    log("done in", round(time.monotonic() - t_start, 2), "s wall; cpu", round(sum(os.times()[:4]), 2), cg)
    # the answers are written (os.write): skip the interpreter teardown (module cleanup, duckdb
    # and nanobind finalizers), which only costs CPU
    sys.stderr.flush()
    os._exit(0)


def run(t_start, deadline, state):
    with open(os.path.join(DATA, "tests.json")) as f:
        tests = json.load(f)["tests"]
    seconds = [t["seconds"] for t in tests]
    commits = sorted(p[:-6] for p in os.listdir(os.path.join(DATA, "commits")) if p.endswith(".patch"))
    state["tests"] = tests
    state["commits"] = commits
    global MAPS_OBJ, REGISTRY_CLEAR_AT
    maps = MAPS_OBJ = Maps(MAPS)
    REGISTRY_CLEAR_AT = registry_clear_at([t["id"] for t in tests])
    all_tests = list(range(len(tests)))

    plans = {}
    for c in commits:
        try:
            with open(os.path.join(DATA, "commits", c + ".patch"), encoding="utf-8", errors="surrogateescape") as f:
                text = f.read()
            plans[c] = make_plan(c, text, maps)
            plans[c].keeps = compute_keeps(plans[c], maps, tests)
        except Exception as e:  # noqa: BLE001
            log(c, "plan failed", repr(e))
            p = plans[c] = Plan(c)
            p.mode = "all"
    log("planned", len(plans), "in", round(time.monotonic() - t_start, 3))

    if os.environ.get("BR_PLAN_ONLY"):
        with open(os.environ["BR_PLAN_ONLY"], "w") as f:
            json.dump({c: {"mode": p.mode, "cands": sorted(p.cands), "note": p.note,
                           "files": sorted(p.files)} for c, p in plans.items()}, f)
        return

    import preload

    preload.install_environment(ROOT)
    answers = state["answers"]
    debug_rows = {}
    tests_of_file = {}
    for t in tests:
        tests_of_file.setdefault(t["file"], []).append(t["number"])

    fixture_readers = {}

    def fixtures_of(n):
        """The .sql fixture files that test n reads."""
        if not fixture_readers:
            for path, sid in maps.meta["opened"].items():
                if path.startswith("tests/fixtures/") and path.endswith(".sql"):
                    for t in maps.tests(sid):
                        fixture_readers.setdefault(t, set()).add(path)
            fixture_readers.setdefault(-1, set())
        return fixture_readers.get(n) or frozenset()

    def finish(c, plan, verdicts, status, ran, held, blind=()):
        failing = {n for n, bad in verdicts.items() if bad}
        selected = set(failing)
        selected.update(blind)
        selected.update(plan.blind)
        unknown = [n for n in ran if n not in verdicts and n not in set(blind)]
        if unknown:
            selected.update(unknown)  # the child ended early: nothing is known about these
        if plan.all_fail:
            selected = set(all_tests)
        # Tests that were too costly to run have no verdict. They are selected, with one
        # exception: when the cheaper tests on the same code all ran and all passed, a very
        # long test is left out (its selection would cost more than the small risk).
        # tests that the commit breaks. A very long test (the TPC-DS run) is left out unless a
        # failing test is close to it: same test class, or same fixture file.
        if "import state unchanged" in plan.note and not failing and not plan.broken and not unknown:
            pass  # the import left nothing different and the cheapest candidates pass
        elif plan.broken or not verdicts or unknown:
            selected.update(held)
        else:
            # On labeled mutants, a long test fails together with another test of its class
            # (or of its fixture file) in nearly every case, and almost never without one.
            # The tests of its class did run (see protect below), so their verdicts decide.
            class_of = lambda n: tests[n]["id"].rsplit("::", 1)[0]
            fail_classes = {class_of(n) for n in failing}
            fail_fixtures = set()
            for n in failing:
                fail_fixtures.update(fixtures_of(n))
            # tests of each class that have no verdict (held, or selected without a run)
            unseen = {}
            for n in selected | set(held):
                if n not in verdicts:
                    unseen[class_of(n)] = unseen.get(class_of(n), 0) + 1
            for n in held:
                if seconds[n] <= QUIET_HOLD_SECONDS:
                    selected.add(n)
                elif class_of(n) in fail_classes or fail_fixtures & fixtures_of(n):
                    selected.add(n)
                elif len(failing) >= MANY_FAILURES:
                    selected.add(n)  # a commit that breaks this much can break any test
                elif unseen.get(class_of(n), 0) > UNSEEN_CLASS and seconds[n] <= UNSEEN_HOLD_SECONDS:
                    # Many tests of the class have no verdict, so the passes of the others
                    # say little about this class. Only the longest tests are still left out.
                    selected.add(n)
        for n in plan.pool_died:
            cls_id = tests[n]["id"].rsplit("::", 1)[0]
            for t in maps.tests(maps.meta.get("pool_users", 0)):
                if t > n and tests[t]["id"].rsplit("::", 1)[0] == cls_id:
                    selected.add(t)
        selected.update(patchrules.importer_tests(maps, plan.broken))
        for name in plan.broken:  # a test file that cannot be imported: all its tests fail
            selected.update(tests_of_file.get(name.replace(".", "/") + ".py", ()))
        answers[c] = pack(sorted(selected), seconds)
        if DEBUG:
            debug_rows[c] = {
                "mode": plan.mode, "status": status, "cands": sorted(plan.cands), "cpu": round(sum(os.times()[:4]), 3),
                "verdicts": {str(k): int(v) for k, v in verdicts.items()}, "note": plan.note,
                "reduced": sorted(plan.keeps), "held": sorted(held), "ran": len(ran), "diff": plan.diff,
                "times": {str(k): round(v, 4) for k, v in plan.times.items()},
                "costs": {str(n): round(estimate_cost(n, plan, maps, seconds), 4) for n in ran},
                "est": round(sum(estimate_cost(n, plan, maps, seconds) for n in ran), 3),
            }

    def choose(plan):
        """Splits the candidates into tests to run and tests that are held (not run)."""
        cands = sorted(plan.cands - plan.blind)
        cost = {n: estimate_cost(n, plan, maps, seconds) for n in cands}
        if RUN_ALL:
            return [n for n in cands if cost[n] <= 60 or RUN_SLOW], [], cost
        # a test is held (not run, selected as it is) when a run costs more CPU than
        # its selection costs in the worst case
        limit = {n: max(HOLD_SECONDS, HOLD_FACTOR * (WORKER_COST + seconds[n])) for n in cands}
        ran = [n for n in cands if cost[n] <= limit[n]]
        held = [n for n in cands if cost[n] > limit[n]]
        budget = COMMIT_SECONDS if plan.mode == "hot" else FRESH_SECONDS
        if "import state unchanged" in plan.note:
            budget = INERT_SECONDS
        # the verdicts of these tests decide about the long held tests: they run first
        long_held = [n for n in held if seconds[n] > QUIET_HOLD_SECONDS]
        protect_classes = {tests[n]["id"].rsplit("::", 1)[0] for n in long_held}
        protect_fixtures = set()
        for n in long_held:
            protect_fixtures.update(fixtures_of(n))
        plan.protect = {
            n for n in ran
            if tests[n]["id"].rsplit("::", 1)[0] in protect_classes
            or (protect_fixtures and protect_fixtures & fixtures_of(n) and cost[n] <= 2.0)
        }
        if sum(cost[n] for n in ran) > budget:
            # The CPU limit does not allow every run. A held test is selected as it is, which
            # costs its recorded seconds: so the runs that save the most recorded seconds per
            # CPU second go first, and the tests that are cheap to select are the ones held.
            keep_set = set()
            acc = 0.0
            for n in sorted(ran, key=lambda n: (n not in plan.protect, -(seconds[n] + 0.002) / (cost[n] + 0.002), cost[n])):
                if acc + cost[n] > budget:
                    continue
                acc += cost[n]
                keep_set.add(n)
            held += [n for n in ran if n not in keep_set]
            ran = [n for n in ran if n in keep_set]
        return ran, held, cost

    shared_info = []
    attr_reads = {}

    def class_fixture_rule(plan):
        """A commit that changes only data files, one of them a .sql fixture whose entries a
        shared class fixture holds in a list (TestExecutor: the TPC-H and TPC-DS queries).
        setUpClass reads the file for every test of the class, but only the tests that read
        that list can see the change: the others leave the candidates, and the readers run
        with the changed entries only."""
        changed = {rel for rel, text in plan.files.items() if rel.startswith("tests/fixtures/") and text is not None}
        if not changed or len(changed) != sum(1 for rel in plan.files if rel.startswith("tests/fixtures/")):
            return
        if not shared_info:
            shared_info.extend(preload.shared_class_info(ROOT) or [None])
            try:
                # which of those lists each test of the class reads (build_attr_reads.py)
                with open(os.path.join(MAPS, "class_attr_reads.json")) as f:
                    attr_reads.update(json.load(f))
            except (OSError, ValueError):
                pass
        for info in shared_info:
            if info is None:
                continue
            prefix, cls, by_file, other = info
            hit = changed & set(by_file)
            if not hit or changed & other:
                continue
            for n in sorted(plan.cands):
                test_id = tests[n]["id"]
                if not test_id.startswith(prefix):
                    continue
                read = attr_reads.get(test_id)
                if read is None:
                    continue  # not in the map: nothing is known about this test
                if any(by_file[f] in read for f in hit):
                    plan.fixture_users.add(n)
                elif not (fixtures_of(n) & changed) - hit:
                    plan.cands.discard(n)

    def execute(c, plan, index):
        box = {}
        if plan.mode == "hot" and not plan.work and not RUN_ALL:
            class_fixture_rule(plan)

        def prepare():
            ran, held, cost = choose(plan)
            box["ran"], box["held"] = ran, held
            allowed = {n: max(TEST_TIMEOUT, 25.0 * cost[n]) * SLOWDOWN for n in ran}
            # Once a test has failed, the commit is known to break something. A further
            # verdict then only saves the recorded seconds of a passing test, so only runs
            # that are cheap next to those seconds go on; the others are selected as they are.
            skip = set() if RUN_ALL else {
                n for n in ran if cost[n] > max(0.03, AFTER_FAIL_RATIO * seconds[n]) and n not in plan.protect
            }
            # A failing test of the class (or fixture) of a long held test selects that long
            # test (see finish). When failures have selected every long held test, and the
            # longest of them is longer than all tests that run together, the tests that are
            # still to run fit beside it on one more worker, which costs 2 s at most. So when
            # the runs that are left cost more CPU than that, they end there and the tests
            # are selected as they are. The answer then holds every candidate without a pass.
            plan.enders = {}
            long_held = [t for t in held if seconds[t] > QUIET_HOLD_SECONDS]
            if long_held and not RUN_ALL and max(seconds[t] for t in long_held) >= sum(seconds[n] for n in ran):
                plan.enders["held"] = set(long_held)
                left_cost = sum(cost[n] for n in ran)
                for n in ran:
                    left_cost -= cost[n]
                    n_class, n_fixtures = tests[n]["id"].rsplit("::", 1)[0], fixtures_of(n)
                    decides = {
                        t for t in long_held
                        if tests[t]["id"].rsplit("::", 1)[0] == n_class or n_fixtures & fixtures_of(t)
                    }
                    if decides:
                        plan.enders[n] = (decides, left_cost > WORKER_COST)
            # A guard against runs that cost many times their estimate (a patch that makes every
            # test slow without hanging): past the cap, the tests without a verdict are selected.
            # The longest per-test limit is added, so that one hung test still counts as a failure.
            cap = None
            if ran and not RUN_ALL:
                cap = SLOWDOWN * (2.0 + 3.0 * sum(cost[n] for n in ran)) + max(allowed.values())
            return ran, allowed, skip, cap

        left = deadline - time.monotonic()
        if left < 3.0:
            if plan.mode != "hot" and any(p.endswith(".py") for p in plan.files):
                # an import-time change that never ran: a broken import can fail any test
                plan.all_fail = True
            ran, held, _ = choose(plan)
            finish(c, plan, {}, "DEADLINE", ran, held)
            return
        use_diff = plan.mode != "hot" and plan.diffable and BASE_STATE is not None and not NO_DIFF
        if use_diff:
            def on_diff(keys):
                refine_with_diff(plan, keys, maps, tests)
                ran, allowed, skip, cap = prepare()
                return {"order": ran, "keeps": plan.keeps, "skip": skip, "allowed": allowed, "cap": cap, "enders": plan.enders}

            verdicts, status, hung = run_child(plan, None, tests, index, {}, left - 2.0, (), on_diff)
            if "ran" not in box:
                # the child never reported its import state: nothing is known
                ran, held, _ = choose(plan)
                if status in ("TIMEOUT", "DIED"):
                    for p in plan.files:
                        if p.endswith(".py"):
                            plan.broken.append(p[:-3].replace("/", "."))
                    plan.note.append("import does not end")
                finish(c, plan, {}, status, ran, held)
                return

        else:
            ran, allowed, skip, cap = prepare()
            if not ran:
                if plan.mode != "hot" and any(p.endswith(".py") for p in plan.files):
                    # No test to run, but the patched tree can still fail to import (a broken
                    # import line, a name that is gone): the child reports the modules that
                    # raise, and finish() selects the tests that load them.
                    _, status, _ = run_child(plan, [], tests, index, {}, left - 2.0)
                    if status in ("TIMEOUT", "DIED"):
                        plan.broken.extend(p[:-3].replace("/", ".") for p in plan.files if p.endswith(".py"))
                        plan.note.append("import does not end")
                finish(c, plan, {}, "END", ran, box["held"])
                return
            verdicts, status, hung = run_child(plan, ran, tests, index, allowed, left - 2.0, skip, None, cap)
            if status == "REDO":
                return "REDO"
            if status in ("TIMEOUT", "DIED") and hung is None and not verdicts and plan.mode != "hot":
                for p in plan.files:
                    if p.endswith(".py"):
                        plan.broken.append(p[:-3].replace("/", "."))
                plan.note.append("import does not end")
        ran, held = box["ran"], box["held"]
        if status == "OVER":
            plan.note.append("over the time cap")
        # a test that does not end is a timeout in the full run; the tests after it run again
        for attempt in range(2):
            if hung is None:
                break
            verdicts[hung] = True
            todo = [n for n in ran if n not in verdicts and n not in plan.skipped]
            left = deadline - time.monotonic()
            if not todo or left < 3.0:
                break
            cost_allowed = {n: TEST_TIMEOUT * SLOWDOWN for n in todo}
            v, status, hung = run_child(plan, todo, tests, index, cost_allowed, left - 2.0, set(todo))
            verdicts.update(v)
        blind = [n for n in ran if n in plan.skipped and n not in verdicts]
        if plan.pool_died:
            first = min(plan.pool_died)
            cls_id = tests[first]["id"].rsplit("::", 1)[0]
            users = set(maps.tests(maps.meta.get("pool_users", 0)))
            blind += [n for n in ran if n not in verdicts and n > first and n in users
                      and tests[n]["id"].rsplit("::", 1)[0] == cls_id]
        finish(c, plan, verdicts, status, ran, held, blind)

    # 1. answers that need no run
    hot, fresh = [], []
    for c in commits:
        plan = plans[c]
        if plan.mode == "all":
            answers[c] = pack(all_tests, seconds)
        elif plan.mode == "blind":
            answers[c] = pack(sorted(plan.blind), seconds)
            if DEBUG:
                debug_rows[c] = {"mode": "blind", "cands": sorted(plan.blind), "verdicts": {}, "note": plan.note}
        elif plan.mode == "noop":
            answers[c] = []
            if DEBUG:
                debug_rows[c] = {"mode": "noop", "cands": [], "verdicts": {}, "note": plan.note}
        elif plan.mode == "hot":
            hot.append(c)
        else:
            fresh.append(c)

    def planned_cost(c):
        plan = plans[c]
        return sum(min(estimate_cost(n, plan, maps, seconds), HOLD_SECONDS * 2) for n in plan.cands)

    hot.sort(key=planned_cost)
    fresh.sort(key=planned_cost)
    need_heavy = any(
        tests[n]["file"] in ("tests/test_optimizer.py", "tests/test_executor.py")
        for c in hot + fresh for n in plans[c].cands
    )
    if need_heavy:
        import duckdb  # noqa: F401
        import pandas  # noqa: F401
        import numpy  # noqa: F401

    def exec_prebuild(hot_commits):
        """TestExecutor attributes that more than one child may read are built once here
        (see preload.py); the rest is built by the child that reads it, if any."""
        cls = getattr(sys.modules.get(preload.EXEC_KEY[0]), preload.EXEC_KEY[1], None)
        users = preload.exec_attribute_users(cls) if cls is not None else {}
        need = collections.Counter()
        for c in hot_commits:
            plan = plans[c]
            names = set()
            for n in plan.cands:
                path, cls_name, meth = tests[n]["id"].split("::")
                attrs = users.get(meth) if (path[:-3].replace("/", "."), cls_name) == preload.EXEC_KEY else None
                if attrs and (RUN_ALL or estimate_cost(n, plan, maps, seconds) <= max(
                        HOLD_SECONDS, HOLD_FACTOR * (WORKER_COST + seconds[n]))):
                    names.update(attrs)
            need.update(names)
        out = ["tpch_sqls", "tpcds_sqls"]
        out += [a for a in preload.EXEC_LAZY if a not in out and need[a] >= 2]
        return out

    # 2. One parent process serves every commit. It imports sqlglot and the needed test
    #    modules once. Commits that only change function bodies run first (cheap: the child
    #    replaces code objects). Commits that change import-time code run after them: their
    #    child drops the loaded sqlglot modules and imports the patched tree again.
    index = None
    global BASE_STATE, BASE_FUNCTION_KEYS
    want_diff = bool(fresh) and not NO_DIFF and any(plans[c].diffable for c in fresh)
    if hot or want_diff:
        preload.import_sqlglot()
    if want_diff:
        # The import state of the pinned tree, to compare each patched import with. It is
        # taken before any test module is loaded, because tests change module state.
        import statefp

        BASE_STATE, base_fpr = statefp.snapshot()
        BASE_FUNCTION_KEYS = base_fpr.function_keys
    if hot:
        mods = sorted({tests[n]["file"][:-3].replace("/", ".") for c in hot for n in plans[c].cands})
        preload.import_tests(mods)
        import reduce

        reduce.install_loaders()
        wanted = set()
        for c in hot:
            if not any(p.startswith("tests/") and p.endswith(".py") for p in plans[c].files):
                for n in plans[c].cands:
                    path, cls_name, _ = tests[n]["id"].split("::")
                    wanted.add((path[:-3].replace("/", "."), cls_name))
        shared = preload.build_class_fixtures(wanted, exec_lazy=True, exec_prebuild=exec_prebuild(hot))
        log("shared fixtures", shared)
        index = engine.FunctionIndex(ROOT)
        gc.collect()
        gc.freeze()
    purge = bool(hot) or want_diff
    def execute_safe(c, plan, index):
        """A failure on the parent side ends only this commit, with a safe answer: its
        candidates, or every test when an import-time change was not seen to its end."""
        try:
            return execute(c, plan, index)
        except Exception as e:  # noqa: BLE001
            log(c, "execute failed", repr(e))
            if plan.mode == "hot":
                safe = plan.cands | plan.blind | patchrules.importer_tests(maps, plan.broken)
            else:
                safe = all_tests
            answers[c] = pack(sorted(safe), seconds)

    for c in hot:
        if execute_safe(c, plans[c], index) == "REDO":
            fresh.append(c)
            plan = plans[c]
            plan.note.append("redo")
            plan.diffable = False
            for p in plan.files:
                rec = maps.file(p)
                if rec is not None:
                    plan.cands.update(maps.tests(rec[7]))
                    plan.cands.update(maps.tests(rec[8]))
            plan.keeps = {n: k for n, k in plan.keeps.items() if k[4] is None}
    for c in fresh:
        plan = plans[c]
        plan.mode = "fresh_purge" if purge else "fresh"
        execute_safe(c, plan, None)

    if DEBUG:
        with open(DEBUG, "w") as f:
            json.dump(debug_rows, f)


if __name__ == "__main__":
    main()
