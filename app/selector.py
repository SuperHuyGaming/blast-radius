"""Blast Radius selector.

For each commit: find the tests that can be affected by the patch (maps built from the pinned
commit), apply the patch in a forked child process, run those tests, and select the tests
that fail. Logs go to stderr; only the answers go to stdout.
"""
from __future__ import annotations

import gc
import json
import marshal
import os
import zlib
import select
import signal
import sys
import time
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine  # noqa: E402

ROOT = os.environ.get("BR_ROOT", "/repo")
DATA = os.environ.get("BR_DATA", "/data")
MAPS = os.environ.get("BR_MAPS", os.path.join(HERE, "maps"))
DEBUG = os.environ.get("BR_DEBUG")
RUN_ALL = os.environ.get("BR_RUN_ALL")  # validation mode: run every candidate, no early stop
RUN_SLOW = os.environ.get("BR_RUN_SLOW")  # validation mode: also run the slow tests
NO_REDUCE = os.environ.get("BR_NO_REDUCE")  # validation mode: always run whole tests
MAPS_OBJ = None
ANSWER_FD = None

WORKER_COST = 2.0
HOLD_SECONDS = float(os.environ.get("BR_HOLD", "1.5"))  # test runs below this CPU cost always start
COMMIT_SECONDS = float(os.environ.get("BR_COMMIT", "8.0"))  # CPU limit of the test runs of one commit
HOLD_FACTOR = float(os.environ.get("BR_HOLD_FACTOR", "0.7"))
QUIET_HOLD_SECONDS = float(os.environ.get("BR_QUIET_HOLD", "5.0"))
TEST_TIMEOUT = 4.0  # a test that runs longer than this (and 25 times its usual time) counts as hung
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
    __slots__ = ("commit", "mode", "cands", "work", "files", "note", "all_fail", "blind", "lines", "keeps", "broken")

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
            if _blank_or_comment(added) and _blank_or_comment(removed) and (nl > 2 or not added):
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
            return True
        if all(engine.func_code_equal(x, y) for x, y in pairs):
            continue  # comments or layout only
        simple = True
        for g in groups:
            o, nr, nl, na, removed, added = g
            if nr != na or any(_indent(x) != _indent(y) for x, y in zip(removed, added)):
                simple = False
        if simple:
            for g in groups:
                for ln in range(g[0], g[0] + g[1]):
                    if stmt_pex[ln]:
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
    n = len(def_set) - 2
    complete = True
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
            elif stmt_set[ln]:
                plan.cands.update(maps.tests(stmt_set[ln]))
            elif nr:
                complete = False
    if not complete:
        plan.cands.update(maps.tests(rec[7]))
        plan.cands.update(maps.tests(rec[8]))
        plan.note.append("import-time fallback")


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
        if is_py and in_tree:
            if fp.status != "modify":
                fresh = True
                rec = maps.file(fp.old_path) if fp.old_path else None
                if rec is not None:
                    plan.cands.update(maps.tests(rec[7]))
                    plan.cands.update(maps.tests(rec[8]))
            elif plan_python_file(plan, fp, maps, old_text):
                fresh = True
                plan.lines.pop(fp.old_path, None)
                rec = maps.file(fp.old_path)
                if rec is not None:
                    add_import_time_candidates(plan, fp, rec, maps)
        # every test that reads the file as data can change
        for key in {fp.old_path, path}:
            if key:
                sid = maps.meta["opened"].get(key)
                if sid:
                    plan.cands.update(maps.tests(sid))
        if fp.status != "modify":
            d = os.path.dirname(path)
            sid = maps.meta["listed"].get(d)
            if sid:
                plan.cands.update(maps.tests(sid))
    if fresh:
        plan.mode = "fresh"
    elif plan.work or plan.cands:
        plan.mode = "hot"
    return plan


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
    if plan.mode not in ("hot", "fresh"):
        return keeps
    if any(p.startswith("tests/") and p.endswith(".py") for p in plan.files):
        return keeps
    for n in plan.cands:
        if tests[n]["id"] in SCALED_IDS:
            keeps[n] = (None, None, None, True, None)
    if plan.mode != "hot":
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
        chosen = set()
        if code_lines:
            seen = False
            whole = False
            for rel, ln in code_lines:
                pat = maps.unit_entries(rel, ln).get(n)
                if pat is None:
                    continue
                seen = True
                if pat == BASE_PATTERN:
                    whole = True
                    break
                chosen.update(patterns[pat])
            if whole:
                continue
            if not seen and plan.work:
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
                continue
        out[n] = (keep_files, keep_tasks)
    return out


# ---------------------------------------------------------------------------
# Children


def child_main(plan: Plan, order, tests, wfd, index, stop_after_fail):
    import runner

    def emit(number, failed):
        os.write(wfd, b"%d %d\n" % (number, 1 if failed else 0))

    def started(number):
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

            for name in preload.broken_modules():
                os.write(wfd, b"B %s\n" % name.encode())
        else:
            for full, pairs in plan.work:
                swaps = engine.plan_swaps(pairs, index)
                if swaps is None:
                    os.write(wfd, b"REDO\n")
                    os._exit(0)
                for f, code in swaps:
                    f.__code__ = code
        read_old = engine.install_open_overlay(ROOT, plan.files)
        import reduce

        keeps = None
        if plan.mode != "hot" and plan.keeps and not NO_REDUCE:
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
            if plan.keeps and not NO_REDUCE:
                keeps = apply_keeps(plan, MAPS_OBJ, tests)
        t_setup = time.process_time()
        if os.environ.get("BR_PROFILE"):
            import cProfile
            import pstats

            prof = cProfile.Profile()
            prof.enable()
            runner.run_tests([(n, tests[n]["id"]) for n in order], emit, stop_after_fail, keeps, started)
            prof.disable()
            pstats.Stats(prof, stream=sys.stderr).sort_stats("cumulative").print_stats(45)
        else:
            runner.run_tests([(n, tests[n]["id"]) for n in order], emit, stop_after_fail, keeps, started)
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


def run_child(plan, order, tests, index, stop_after_fail, allowed, time_left):
    """Runs the tests in a forked child. Returns (verdicts, status, hung test or None).

    status: END, REDO, ERR, TIMEOUT, DIED. allowed[n] = seconds that test n may take."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            os.setpgid(0, 0)
        except OSError:
            pass
        child_main(plan, order, tests, w, index, stop_after_fail)
        os._exit(0)
    os.close(w)
    verdicts = {}
    status = "DIED"
    hung = None
    broken = plan.broken
    buf = b""
    t_end = time.monotonic() + time_left
    current = None
    t_test = time.monotonic() + 30.0  # time for the child's own setup
    while True:
        now = time.monotonic()
        if now >= t_end:
            status = "TIMEOUT"
            break
        if now >= t_test:
            status = "TIMEOUT"
            hung = current
            break
        ready, _, _ = select.select([r], [], [], max(0.01, min(t_end, t_test) - now))
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
                t_test = time.monotonic() + allowed.get(current, 30.0)
            elif line.startswith(b"B "):
                broken.append(line[2:].decode())
            else:
                a, b = line.split()
                verdicts[int(a)] = b == b"1"
                current = None
                t_test = time.monotonic() + 30.0
    os.close(r)
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


def estimate_cost(n, plan: Plan, maps: Maps, seconds):
    """Expected CPU seconds of running test n for this commit (whole or reduced)."""
    whole = maps.cost[n] if maps.cost else seconds[n] * 1.5
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


def run(t_start, deadline, state):
    with open(os.path.join(DATA, "tests.json")) as f:
        tests = json.load(f)["tests"]
    seconds = [t["seconds"] for t in tests]
    commits = sorted(p[:-6] for p in os.listdir(os.path.join(DATA, "commits")) if p.endswith(".patch"))
    state["tests"] = tests
    state["commits"] = commits
    global MAPS_OBJ
    maps = MAPS_OBJ = Maps(MAPS)
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

    def finish(c, plan, verdicts, status, ran, held):
        failing = {n for n, bad in verdicts.items() if bad}
        selected = set(failing)
        unknown = [n for n in ran if n not in verdicts]
        if unknown:
            selected.update(unknown)  # the child ended early: nothing is known about these
        if plan.all_fail:
            selected = set(all_tests)
        # Tests that were too costly to run have no verdict. They are selected, with one
        # exception: when the cheaper tests on the same code all ran and all passed, a very
        # long test is left out (its selection would cost more than the small risk).
        if selected or plan.broken or not verdicts:
            selected.update(held)
        else:
            selected.update(n for n in held if seconds[n] <= QUIET_HOLD_SECONDS)
        for name in plan.broken:
            rel = name.replace(".", "/")
            for cand in (rel + ".py", rel + "/__init__.py"):
                sid = maps.meta["importers"].get(cand)
                if sid:
                    selected.update(maps.tests(sid))
        answers[c] = pack(sorted(selected), seconds)
        if DEBUG:
            debug_rows[c] = {
                "mode": plan.mode, "status": status, "cands": sorted(plan.cands), "cpu": round(sum(os.times()[:4]), 3),
                "verdicts": {str(k): int(v) for k, v in verdicts.items()}, "note": plan.note,
                "reduced": sorted(plan.keeps), "held": sorted(held), "ran": len(ran),
                "est": round(sum(estimate_cost(n, plan, maps, seconds) for n in ran), 3),
            }

    def execute(c, plan, index):
        cands = sorted(plan.cands)
        cost = {n: estimate_cost(n, plan, maps, seconds) for n in cands}
        if RUN_ALL:
            ran, held = [n for n in cands if cost[n] <= 60 or RUN_SLOW], []
        else:
            # a test is held (not run, selected as it is) when a run costs more CPU than
            # its selection costs in the worst case
            limit = {n: max(HOLD_SECONDS, HOLD_FACTOR * (WORKER_COST + seconds[n])) for n in cands}
            ran = [n for n in cands if cost[n] <= limit[n]]
            held = [n for n in cands if cost[n] > limit[n]]
            total = sum(cost[n] for n in ran)
            if total > COMMIT_SECONDS:
                # keep the cheapest tests up to the limit; the others are held
                keep_set = set()
                acc = 0.0
                for n in sorted(ran, key=lambda n: cost[n]):
                    if acc + cost[n] > COMMIT_SECONDS:
                        break
                    acc += cost[n]
                    keep_set.add(n)
                held += [n for n in ran if n not in keep_set]
                ran = [n for n in ran if n in keep_set]
        if not ran:
            if held and not RUN_ALL and plan.mode in ("hot", "fresh", "fresh_purge"):
                plan.note.append("only held tests")
            finish(c, plan, {}, "END", ran, held)
            return
        allowed = {n: max(TEST_TIMEOUT, 25.0 * cost[n]) * SLOWDOWN for n in ran}
        verdicts = {}
        status = "END"
        todo = ran
        for attempt in range(3):
            left = deadline - time.monotonic()
            if left < 3.0:
                status = "DEADLINE"
                break
            v, status, hung = run_child(plan, todo, tests, index, False, allowed, left - 2.0)
            if status == "REDO":
                plan.mode = "fresh"
                return "REDO"
            verdicts.update(v)
            if hung is not None:
                verdicts[hung] = True  # a test that does not end is a timeout in the full run
            todo = [n for n in todo if n not in verdicts]
            if status == "END" or not todo or hung is None:
                break
        finish(c, plan, verdicts, status, ran, held)

    # 1. commits that need a fresh import run first, from a parent that has not imported sqlglot
    hot = []
    for c in commits:
        plan = plans[c]
        if plan.mode == "all":
            answers[c] = pack(all_tests, seconds)
        elif plan.mode == "noop":
            answers[c] = []
            if DEBUG:
                debug_rows[c] = {"mode": "noop", "cands": [], "verdicts": {}, "note": plan.note}
        elif plan.mode == "hot":
            hot.append(c)
    need_heavy = any(
        tests[n]["file"] in ("tests/test_optimizer.py", "tests/test_executor.py")
        for c in commits for n in plans[c].cands
    )
    if need_heavy:
        import duckdb  # noqa: F401
        import pandas  # noqa: F401
        import numpy  # noqa: F401
    for c in commits:
        if plans[c].mode == "fresh":
            execute(c, plans[c], None)

    # 2. the other commits share one parent that has sqlglot imported
    redo = []
    if hot:
        preload.import_sqlglot()
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
        shared = preload.build_class_fixtures(wanted)
        log("shared fixtures", shared)
        index = engine.FunctionIndex(ROOT)
        gc.collect()
        gc.freeze()
        for c in hot:
            if execute(c, plans[c], index) == "REDO":
                redo.append(c)
    for c in redo:
        # the parent has sqlglot loaded now; a fresh child must drop those modules first
        plan = plans[c]
        plan.note.append("redo")
        rec_cands = set(plan.cands)
        for p in plan.files:
            rec = maps.file(p)
            if rec is not None:
                rec_cands.update(maps.tests(rec[7]))
                rec_cands.update(maps.tests(rec[8]))
        plan.cands = rec_cands
        plan.mode = "fresh_purge"
        execute(c, plan, None)

    if DEBUG:
        with open(DEBUG, "w") as f:
            json.dump(debug_rows, f)


if __name__ == "__main__":
    main()
