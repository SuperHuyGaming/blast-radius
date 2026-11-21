"""pytest plugin: per-phase line coverage of sqlglot/ and tests/, and per-phase file opens.

Contexts: "<import>" (outside collection/tests), "C|<file>" (collecting a test file),
"S|<nodeid>" / "R|<nodeid>" / "T|<nodeid>" (setup / call / teardown of a test).
Writes MAP_OUT/cov.json {"contexts": [...], "files": {file: {line: [ctx idx]}}},
opens.json {ctx: [paths]}, outcomes.json {nodeid: outcome}, order.json [nodeids in run order].
"""
import json, os, sys
import coverage
import pytest

ROOT = os.path.realpath(os.environ.get("MAP_ROOT", "/src"))
PROBES = os.environ.get("MAP_PROBES") == "1"      # operator probes (no coverage)
COVER = os.environ.get("MAP_COVERAGE", "1") == "1"  # coverage, class use, opens
OUT = os.environ.get("MAP_OUT", "/out")
cov = coverage.Coverage(data_file=None, branch=False,
                        source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")])
current = ["<import>"]
opens: dict = {}
outcomes: dict = {}
order: list = []


def _set(ctx):
    current[0] = ctx
    if COVER:
        cov.switch_context(ctx)


def _audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        try:
            p = os.fsdecode(args[0])
        except Exception:
            return
        if not os.path.isabs(p):
            p = os.path.join(os.getcwd(), p)
        p = os.path.realpath(p)
        if p.startswith(ROOT + os.sep) and not p.endswith(".pyc"):
            opens.setdefault(current[0], set()).add(os.path.relpath(p, ROOT))


sys.addaudithook(_audit)


# Tests that farm work out to spawned processes would hide that work from
# coverage. Run it inline instead, still pickling arguments and results so the
# serialisation code (Expr.__reduce__ -> serde) runs exactly as it would.
import concurrent.futures as _cf
import pickle as _pickle


class InlineExecutor(_cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_pickle.loads(_pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        fut = _cf.Future()
        try:
            fn, args, kwargs = _pickle.loads(_pickle.dumps((fn, args, kwargs)))
            fut.set_result(_pickle.loads(_pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


_cf.ProcessPoolExecutor = InlineExecutor
import concurrent.futures.process as _cfp  # noqa: E402
_cfp.ProcessPoolExecutor = InlineExecutor
# Which sqlglot classes each context used: the type of `self` / `cls` of every
# sqlglot function call. A class-level change can only reach tests that used
# that class or a subclass.
import threading
SQLROOT = os.path.join(ROOT, "sqlglot") + os.sep
types_by_ctx: dict = {}


def _prof(frame, event, arg):
    if event != "call":
        return
    co = frame.f_code
    if co.co_argcount == 0 or not co.co_filename.startswith(SQLROOT):
        return
    v = co.co_varnames[0]
    if v != "self" and v != "cls":
        return
    try:
        o = frame.f_locals[v]
    except Exception:
        return
    t = o if v == "cls" else type(o)
    s = types_by_ctx.get(current[0])
    if s is None:
        s = types_by_ctx[current[0]] = set()
    s.add(t)


# Module bodies run at import time even when the import happens lazily inside a
# test (sqlglot loads dialects on first use). Count that work as "<import>".
import importlib._bootstrap_external as _be

_orig_exec_module = _be._LoaderBasics.exec_module


def _exec_module(self, module):
    f = getattr(module, "__file__", None) or ""
    if not os.path.realpath(f).startswith(SQLROOT):
        return _orig_exec_module(self, module)
    prev = current[0]
    _set("<import>")
    try:
        return _orig_exec_module(self, module)
    finally:
        _set(prev)


_be._LoaderBasics.exec_module = _exec_module


# Comparison probes: every single-operator `is` / `is not` / `==` / `!=` in sqlglot
# is compiled as a call that evaluates it exactly as before and records, per
# context, whether `a is b` and `a == b` ever disagreed at that comparison.
import ast as _ast
import builtins as _builtins

cmp_differ: dict = {}
cmp_seen: set = set()
_CMP_OPS = {_ast.Is: 0, _ast.IsNot: 1, _ast.Eq: 2, _ast.NotEq: 3}


def _mark(key):
    s = cmp_differ.get(key)
    if s is None:
        s = cmp_differ[key] = set()
    s.add(current[0])


def blast_tie_probe_(key, a, b, op):
    """< <= > >= exactly as written; records evaluations where a == b (where < and <= differ)."""
    cmp_seen.add("tie|" + key)
    r = a < b if op == 0 else a <= b if op == 1 else a > b if op == 2 else a >= b
    if _sqlglot_defined(type(a), "__eq__") or _sqlglot_defined(type(b), "__eq__"):
        _mark("tie|" + key)
        return r
    try:
        if a == b:
            _mark("tie|" + key)
    except Exception:
        _mark("tie|" + key)
    return r


def blast_bool_probe_(key, kind, value):
    """Operand of a 2-operand and/or, returned unchanged. For `and` records a falsy
    operand, for `or` a truthy one: the evaluations where flipping the operator could
    change the condition's truth value."""
    k = kind + "|" + key
    cmp_seen.add(k)
    if _sqlglot_defined(type(value), "__bool__", "__len__"):
        _mark(k)
        return value
    try:
        t = bool(value)
    except Exception:
        _mark(k)
        return value
    if (kind == "and" and not t) or (kind == "or" and t):
        _mark(k)
    return value


def _sqlglot_defined(t, *names):
    """True if class t gets one of these dunders from sqlglot code (deep or stateful:
    e.g. Expr.__eq__ computes and caches a tree hash). Probes don't call those."""
    for n in names:
        f = getattr(t, n, None)
        if f is not None and getattr(f, "__module__", "") and str(getattr(f, "__module__", "")).startswith("sqlglot"):
            return True
    return False


def _blast_cmp(key, a, b, op):
    cmp_seen.add("isq|" + key)
    same = a is b
    if same and not (isinstance(a, float) and a != a):  # NaN is the one object != itself
        agree, eq = True, None
    elif _sqlglot_defined(type(a), "__eq__") or _sqlglot_defined(type(b), "__eq__"):
        agree, eq = False, None  # not evaluated: counted as "may differ"
    else:
        try:
            eq = a == b
            agree = bool(eq) == same
        except Exception:
            eq = None
            agree = False
    if not agree:
        key = "isq|" + key
        s = cmp_differ.get(key)
        if s is None:
            s = cmp_differ[key] = set()
        s.add(current[0])
    if op == 0:
        return same
    if op == 1:
        return not same
    if op == 2:
        return a == b if eq is None else eq
    return a != b


_builtins.blast_cmp_probe_ = _blast_cmp
_builtins.blast_tie_probe_ = blast_tie_probe_
_builtins.blast_bool_probe_ = blast_bool_probe_
_TIE_OPS = {_ast.Lt: 0, _ast.LtE: 1, _ast.Gt: 2, _ast.GtE: 3}


class _Probe(_ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if len(node.values) != 2:
            return node
        key = f"{self.rel}:{node.lineno}:{node.col_offset}"
        kind = "and" if isinstance(node.op, _ast.And) else "or"
        node.values = [_ast.copy_location(_ast.Call(func=_ast.Name("blast_bool_probe_", _ast.Load()),
                                                    args=[_ast.Constant(key), _ast.Constant(kind), v], keywords=[]), v)
                       for v in node.values]
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in _TIE_OPS:
            key = f"{self.rel}:{node.lineno}:{node.col_offset}"
            call = _ast.Call(func=_ast.Name("blast_tie_probe_", _ast.Load()),
                             args=[_ast.Constant(key), node.left, node.comparators[0],
                                   _ast.Constant(_TIE_OPS[type(node.ops[0])])], keywords=[])
            return _ast.copy_location(call, node)
        if len(node.ops) != 1 or type(node.ops[0]) not in _CMP_OPS:
            return node
        key = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = _ast.Call(func=_ast.Name("blast_cmp_probe_", _ast.Load()),
                         args=[_ast.Constant(key), node.left, node.comparators[0],
                               _ast.Constant(_CMP_OPS[type(node.ops[0])])], keywords=[])
        return _ast.copy_location(call, node)


_orig_source_to_code = _be.SourceLoader.source_to_code


def _source_to_code(self, data, path, *, _optimize=-1):
    rp = os.path.realpath(str(path))
    if not rp.startswith(SQLROOT) or not rp.endswith(".py"):
        return _orig_source_to_code(self, data, path, _optimize=_optimize)
    tree = _ast.parse(data, filename=str(path))
    tree = _ast.fix_missing_locations(_Probe(os.path.relpath(rp, ROOT).replace(os.sep, "/")).visit(tree))
    return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


if PROBES:
    _be.SourceLoader.source_to_code = _source_to_code


# Key tracing (MAP_KEYS=1): every dict display assigned to a name in a sqlglot class
# body or module body becomes a dict subclass that records, per context, which keys
# were looked up (hits and misses) and whether the table was iterated. Plain
# `**Other.TABLE` entries in such a display are copied without counting as a read and
# recorded as an alias (keys flow from Other.TABLE into this table). Any other read at
# import time (iteration, copy, key lookups) is recorded too, so the selector can tell
# which tables feed import-time state (tries, derived sets) and leave those alone.
KEYS = os.environ.get("MAP_KEYS") == "1"
import enum as _enum

kt_keys: dict = {}     # name -> {ctx: set(key reprs)}
kt_iter: dict = {}     # name -> set(ctx)
kt_made: set = set()   # names of tables that were created as tracing dicts
kt_alias: set = set()  # (source name, target name)
kt_sites: set = set()  # "name@file:line" of import-time iterations (diagnostics)


def _krepr(k):
    if isinstance(k, str):
        return "s:" + k
    if isinstance(k, type):
        return "t:" + k.__name__
    if isinstance(k, _enum.Enum):
        return "e:" + k.name
    if k is None or isinstance(k, (bool, int, float)):
        return "c:" + repr(k)
    if isinstance(k, tuple):
        return "u:" + "|".join(_krepr(x) for x in k)
    return "?"


class _KT(dict):
    __slots__ = ("_kt",)

    def _k(self, k):
        try:
            r = _krepr(k)
        except Exception:
            r = "?"
        d = kt_keys.get(self._kt)
        if d is None:
            d = kt_keys[self._kt] = {}
        s = d.get(current[0])
        if s is None:
            s = d[current[0]] = set()
        s.add(r)

    def _i(self):
        s = kt_iter.get(self._kt)
        if s is None:
            s = kt_iter[self._kt] = set()
        s.add(current[0])
        c = current[0]
        if c == "<import>" or c.startswith("C|"):
            f = sys._getframe(2)
            kt_sites.add(f"{self._kt}@{os.path.relpath(f.f_code.co_filename, ROOT)}:{f.f_lineno}")

    def __getitem__(self, k):
        self._k(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._k(k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        self._k(k)
        return dict.__contains__(self, k)

    def pop(self, k, *a):
        self._k(k)
        return dict.pop(self, k, *a)

    def setdefault(self, k, default=None):
        self._k(k)
        return dict.setdefault(self, k, default)

    def __setitem__(self, k, v):
        self._k(k)
        return dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        self._k(k)
        return dict.__delitem__(self, k)

    def __iter__(self):
        self._i()
        return dict.__iter__(self)

    def __reversed__(self):
        self._i()
        return dict.__reversed__(self)

    def keys(self):
        self._i()
        return dict.keys(self)

    def items(self):
        self._i()
        return dict.items(self)

    def values(self):
        self._i()
        return dict.values(self)

    def __len__(self):
        self._i()
        return dict.__len__(self)

    def copy(self):
        self._i()
        return dict.copy(self)

    def popitem(self):
        self._i()
        return dict.popitem(self)

    def __or__(self, o):
        self._i()
        return dict.__or__(self, o)

    def __ror__(self, o):
        self._i()
        return dict.__ror__(self, o)

    def __eq__(self, o):
        self._i()
        return dict.__eq__(self, o)

    def __ne__(self, o):
        self._i()
        return dict.__ne__(self, o)

    __hash__ = None

    def __repr__(self):
        self._i()
        return dict.__repr__(self)

    def __reduce_ex__(self, proto):
        self._i()
        return (dict, (dict(dict.items(self)),))


def blast_kt_wrap_(name, d):
    t = _KT(d)
    t._kt = name
    kt_made.add(name)
    return t


def blast_kt_copy_(src, target):
    """A `**src` entry of a traced display: keys flow into `target`, not a read."""
    if isinstance(src, _KT):
        if src._kt != target:
            kt_alias.add((src._kt, target))
        return dict(dict.items(src))
    return src


_builtins.blast_kt_wrap_ = blast_kt_wrap_
_builtins.blast_kt_copy_ = blast_kt_copy_


class _KeyWrap(_ast.NodeTransformer):
    """Wrap `NAME = {...}` / `NAME: T = {...}` in module and class bodies (not in
    functions)."""

    def visit_FunctionDef(self, node):
        return node

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def _wrap(self, name, value):
        for i, k in enumerate(value.keys):
            if k is None:  # **entry
                value.values[i] = _ast.copy_location(_ast.Call(
                    func=_ast.Name("blast_kt_copy_", _ast.Load()),
                    args=[value.values[i], _ast.Constant(name)], keywords=[]), value.values[i])
        return _ast.copy_location(_ast.Call(func=_ast.Name("blast_kt_wrap_", _ast.Load()),
                                            args=[_ast.Constant(name), value], keywords=[]), value)

    def visit_Assign(self, node):
        if (isinstance(node.value, _ast.Dict) and len(node.targets) == 1
                and isinstance(node.targets[0], _ast.Name)):
            node.value = self._wrap(node.targets[0].id, node.value)
        return node

    def visit_AnnAssign(self, node):
        if isinstance(node.value, _ast.Dict) and isinstance(node.target, _ast.Name):
            node.value = self._wrap(node.target.id, node.value)
        return node


def _source_to_code_keys(self, data, path, *, _optimize=-1):
    rp = os.path.realpath(str(path))
    if not rp.startswith(SQLROOT) or not rp.endswith(".py"):
        return _orig_source_to_code(self, data, path, _optimize=_optimize)
    tree = _ast.parse(data, filename=str(path))
    tree = _ast.fix_missing_locations(_KeyWrap().visit(tree))
    return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


if KEYS:
    _be.SourceLoader.source_to_code = _source_to_code_keys
sys.dont_write_bytecode = True

if COVER:
    sys.setprofile(_prof)
    threading.setprofile(_prof)
    cov.start()
_set("<import>")


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    prev = current[0]
    fspath = getattr(collector, "path", None)
    if fspath is not None and str(fspath).endswith(".py"):
        _set("C|" + os.path.relpath(os.path.realpath(str(fspath)), ROOT))
    yield
    _set(prev)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    order.append(item.nodeid)
    _set("S|" + item.nodeid)
    yield
    _set("<import>")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set("R|" + item.nodeid)
    yield
    _set("<import>")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _set("T|" + item.nodeid)
    yield
    _set("<import>")


def pytest_runtest_logreport(report):
    if report.failed:
        outcomes[report.nodeid] = "failed"
    elif report.when == "call" and report.nodeid not in outcomes:
        outcomes[report.nodeid] = report.outcome
    elif report.skipped:
        outcomes.setdefault(report.nodeid, "skipped")


def pytest_sessionfinish(session, exitstatus):
    os.makedirs(OUT, exist_ok=True)
    if PROBES:
        wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
        json.dump({"differ": {k: sorted(v) for k, v in cmp_differ.items()}, "seen": sorted(cmp_seen)},
                  open(os.path.join(OUT, f"cmp_{wid}.json"), "w"))
        json.dump(outcomes, open(os.path.join(OUT, f"probe_outcomes_{wid}.json"), "w"))
    if KEYS:
        wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
        json.dump({"keys": {n: {c: sorted(s) for c, s in d.items()} for n, d in kt_keys.items()},
                   "iter": {n: sorted(s) for n, s in kt_iter.items()},
                   "made": sorted(kt_made), "alias": sorted(kt_alias), "sites": sorted(kt_sites)},
                  open(os.path.join(OUT, f"keys_{wid}.json"), "w"))
        json.dump(outcomes, open(os.path.join(OUT, f"keys_outcomes_{wid}.json"), "w"))
    if not COVER:
        return
    _session_finish_cover()


def _session_finish_cover():
    cov.stop()
    data = cov.get_data()
    ctx_ids: dict = {}
    out: dict = {}
    for f in data.measured_files():
        rel = os.path.relpath(os.path.realpath(f), ROOT).replace(os.sep, "/")
        out[rel] = {ln: sorted(ctx_ids.setdefault(c, len(ctx_ids)) for c in ctxs)
                    for ln, ctxs in data.contexts_by_lineno(f).items()}
    os.makedirs(OUT, exist_ok=True)
    names = [None] * len(ctx_ids)
    for c, i in ctx_ids.items():
        names[i] = c
    json.dump({"contexts": names, "files": out}, open(os.path.join(OUT, "cov.json"), "w"))
    json.dump({k: sorted(v) for k, v in opens.items()}, open(os.path.join(OUT, "opens.json"), "w"))
    json.dump(outcomes, open(os.path.join(OUT, "outcomes.json"), "w"))
    json.dump(order, open(os.path.join(OUT, "order.json"), "w"))
    sys.setprofile(None)
    keys: dict = {}

    def class_keys(t):
        if t not in keys:
            ks = []
            for c in getattr(t, "__mro__", ()):
                mod = sys.modules.get(getattr(c, "__module__", ""), None)
                f = getattr(mod, "__file__", None) or ""
                f = os.path.realpath(f)
                if f.startswith(SQLROOT):
                    ks.append(os.path.relpath(f, ROOT).replace(os.sep, "/") + "::" + c.__qualname__)
            keys[t] = ks
        return keys[t]

    classes: dict = {}
    for ctx, ts in types_by_ctx.items():
        for t in ts:
            for k in class_keys(t):
                classes.setdefault(k, set()).add(ctx)
    json.dump({k: sorted(v) for k, v in classes.items()}, open(os.path.join(OUT, "classes.json"), "w"))
