"""pytest plugin that records what every sqlglot test depends on, at the pinned commit.

For each test it records
  * the source lines (sqlglot/ and tests/) it executes,
  * the repository files it opens and the directories it lists,
  * the sqlglot classes it instantiates,
  * the class attributes it reads, and the keys it looks up in class-level and
    module-level dicts and sets.
It also records which lines run while each module is being imported.

Run from the sqlglot checkout:

    SKIP_INTEGRATION=0 PYTHONPATH=<this dir> python -m pytest tests -p mapplugin -p no:cacheprovider -q

The raw result goes to $MAP_OUT; tools/pack_maps.py turns it into the compact
files the selector loads.  No patch is involved anywhere: everything here is a
property of the pinned commit.
"""

import concurrent.futures as cf
import importlib
import importlib._bootstrap_external as _be
import inspect
import os
import pickle
import pkgutil
import sys
import time
import types

import pytest

from blastkeys import ALL, cname, knorm

REPO = os.getcwd()
OUT = os.environ.get("MAP_OUT", "/tmp/rawmap.pickle")
TRACK = os.environ.get("MAP_TRACK", "1") == "1"
# MAP_PRELOAD=0 records a run where dialects load lazily, as in a plain pytest run;
# it shows which lines only run on the first use of a dialect in a process
PRELOAD = os.environ.get("MAP_PRELOAD", "1") == "1"
TOOL = 4
mon = sys.monitoring
DISABLE = mon.DISABLE

# ---------------------------------------------------------------- state
file_ids = {}
import_stack = []
import_lines = {}  # (rel, line) -> set(module names being imported)
import_phase = True

cur_lines = None
cur_opens = None
cur_classes = None
READS = None  # set of (owner class, attr) | (bag id, key)
collect_opens = {}
cur_collector = None
tests = {}

setup_class = None  # test class whose setUpClass is running
setup_lines = {}  # test class -> {file: lines of setUpClass that opened it}
setup_seen = {}  # test class -> names in its namespace before setUpClass
class_files = {}  # "path::Class" -> {file: attributes holding its data, or None}
bags = {}  # bag id -> list of "module:Class.attr" / "module::name" labels
bag_of = {}  # id(original container) -> wrapper


def _rel(filename):
    try:
        return file_ids[filename]
    except KeyError:
        rel = None
        if filename.startswith(REPO + os.sep):
            r = filename[len(REPO) + 1 :]
            if r.startswith(("sqlglot" + os.sep, "tests" + os.sep)):
                rel = r
        file_ids[filename] = rel
        return rel


def on_line(code, line):
    if import_phase:
        rel = _rel(code.co_filename)
        if rel is None:
            return DISABLE
        if import_stack:
            key = (rel, line)
            s = import_lines.get(key)
            if s is None:
                s = import_lines[key] = set()
            s.add(import_stack[-1])
        return None
    if import_stack:
        # a module being imported lazily during a test (only when not preloaded)
        rel = _rel(code.co_filename)
        if rel is None:
            return DISABLE
        key = (rel, line)
        s = import_lines.get(key)
        if s is None:
            s = import_lines[key] = set()
        s.add(import_stack[-1])
        return None
    s = cur_lines
    if s is not None:
        rel = _rel(code.co_filename)
        if rel is not None:
            s.add((rel, line))
    return DISABLE


_orig_exec = _be._LoaderBasics.exec_module


def _exec_module(self, module):
    name = module.__name__
    if name.startswith(("sqlglot", "tests")):
        import_stack.append(name)
        try:
            return _orig_exec(self, module)
        finally:
            import_stack.pop()
    return _orig_exec(self, module)


def audit(event, args):
    if event == "open" or event == "os.listdir" or event == "os.scandir" or event == "glob.glob":
        path = args[0]
        if path is None:
            path = "."
        if isinstance(path, bytes):
            try:
                path = path.decode()
            except Exception:
                return
        if not isinstance(path, str):
            return
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if path.startswith(REPO + os.sep) or path == REPO:
            rel = path[len(REPO) + 1 :]
            if "__pycache__" in rel or rel.startswith(".git") or rel.endswith(".pyc"):
                return
            if event != "open":
                rel = "dir:" + rel
            elif rel.endswith(".py") and import_stack:
                return  # the import system reading a source file
            if cur_opens is not None:
                cur_opens.add(rel)
                if setup_class is not None:
                    f = sys._getframe(1)
                    while f is not None and f.f_code.co_name != "setUpClass":
                        f = f.f_back
                    if f is not None:
                        setup_lines.setdefault(setup_class, {}).setdefault(rel, set()).add(f.f_lineno)
            elif cur_collector is not None:
                collect_opens.setdefault(cur_collector, set()).add(rel)


# ---------------------------------------------------------------- inline pool
class InlineExecutor:
    """Runs submitted work in this process, with the same pickling round trip a
    spawned worker would do, so one tracer sees everything."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        fut = cf.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = fn(*args, **kwargs)
            res = pickle.loads(pickle.dumps(res))
            fut.set_result(res)
        except BaseException as e:  # noqa
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------- read tracking
class Attr:
    """Class attribute that notes who reads it."""

    __slots__ = ("value", "name")

    def __init__(self, value, name):
        self.value = value
        self.name = name

    def __get__(self, inst, owner=None):
        r = READS
        if r is not None:
            r.add((owner if owner is not None else type(inst), self.name))
        return self.value


def _rec(bid, k):
    r = READS
    if r is not None:
        try:
            r.add((bid, k))
        except TypeError:
            r.add((bid, ALL))


class TDict(dict):
    __slots__ = ("_bid",)

    def __getitem__(self, k):
        _rec(self._bid, k)
        return dict.__getitem__(self, k)

    def get(self, k, d=None):
        _rec(self._bid, k)
        return dict.get(self, k, d)

    def __contains__(self, k):
        _rec(self._bid, k)
        return dict.__contains__(self, k)

    def pop(self, k, *a):
        _rec(self._bid, k)
        return dict.pop(self, k, *a)

    def setdefault(self, k, d=None):
        _rec(self._bid, k)
        return dict.setdefault(self, k, d)

    def __iter__(self):
        _rec(self._bid, ALL)
        return dict.__iter__(self)

    def __len__(self):
        _rec(self._bid, ALL)
        return dict.__len__(self)

    def keys(self):
        _rec(self._bid, ALL)
        return dict.keys(self)

    def values(self):
        _rec(self._bid, ALL)
        return dict.values(self)

    def items(self):
        _rec(self._bid, ALL)
        return dict.items(self)

    def copy(self):
        _rec(self._bid, ALL)
        return dict(dict.items(self))

    def __eq__(self, o):
        _rec(self._bid, ALL)
        return dict.__eq__(self, o)

    def __ne__(self, o):
        _rec(self._bid, ALL)
        return dict.__ne__(self, o)

    __hash__ = None

    def __or__(self, o):
        _rec(self._bid, ALL)
        return dict.__or__(dict(dict.items(self)), o)

    def __ror__(self, o):
        _rec(self._bid, ALL)
        return dict.__or__(dict(o), dict(dict.items(self)))

    def __reversed__(self):
        _rec(self._bid, ALL)
        return dict.__reversed__(self)

    def __repr__(self):
        _rec(self._bid, ALL)
        return dict.__repr__(self)

    def __reduce__(self):
        _rec(self._bid, ALL)
        return (dict, (dict(dict.items(self)),))


def _mk_set(base):
    def allm(name):
        orig = getattr(base, name)

        def f(self, *a):
            _rec(self._bid, ALL)
            return orig(self, *a)

        f.__name__ = name
        return f

    ns = {"__slots__": ("_bid",)}

    def __contains__(self, k):
        _rec(self._bid, k)
        return base.__contains__(self, k)

    ns["__contains__"] = __contains__
    for name in (
        "__iter__ __len__ __eq__ __ne__ __le__ __lt__ __ge__ __gt__ __or__ __ror__ __and__ __rand__ "
        "__sub__ __rsub__ __xor__ __rxor__ union intersection difference symmetric_difference "
        "issubset issuperset isdisjoint copy __repr__"
    ).split():
        ns[name] = allm(name)
    if base is set:
        ns["__hash__"] = None
    else:
        ns["__hash__"] = allm("__hash__")

    def __reduce__(self):
        _rec(self._bid, ALL)
        return (base, (list(base.__iter__(self)),))

    ns["__reduce__"] = __reduce__
    return type("T" + base.__name__.capitalize(), (base,), ns)


TSet = _mk_set(set)
TFrozenset = _mk_set(frozenset)


def _wrap_container(v, label):
    w = bag_of.get(id(v))
    if w is None:
        t = type(v)
        if t is dict:
            w = TDict(v)
        elif t is set:
            w = TSet(v)
        elif t is frozenset:
            w = TFrozenset(v)
        else:
            return None
        w._bid = len(bags)
        bags[w._bid] = []
        bag_of[id(v)] = w
        _keep.append(v)
    bags[w._bid].append(label)
    return w


_keep = []
class_info = {}  # cname -> dict(mro=[cname...], file=rel, attrs=[names in own dict])


def _all_classes():
    seen = {}
    stack = []
    for name, mod in list(sys.modules.items()):
        if mod is None or not name.startswith("sqlglot"):
            continue
        for obj in list(vars(mod).values()):
            if inspect.isclass(obj) and getattr(obj, "__module__", "").startswith("sqlglot"):
                stack.append(obj)
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen[id(c)] = c
        for v in list(vars(c).values()):
            if inspect.isclass(v) and getattr(v, "__module__", "").startswith("sqlglot"):
                stack.append(v)
        if issubclass(c, type):
            continue
        for s in type.__subclasses__(c):
            if getattr(s, "__module__", "").startswith("sqlglot"):
                stack.append(s)
    return list(seen.values())


SKIP_ATTRS = {"__module__", "__qualname__", "__doc__", "__dict__", "__weakref__", "__slots__",
              "__annotations__", "__firstlineno__", "__static_attributes__", "__parameters__",
              "__orig_bases__", "__match_args__", "__dataclass_fields__", "__dataclass_params__",
              "__abstractmethods__", "_abc_impl", "__hash__", "__classcell__", "__type_params__"}


def _install_tracking():
    import enum

    classes = _all_classes()
    for c in classes:
        class_info[cname(c)] = dict(
            mro=[cname(b) for b in c.__mro__ if getattr(b, "__module__", "").startswith("sqlglot")],
            module=c.__module__,
        )
    # users: record the concrete class of every sqlglot object that gets built
    for c in classes:
        init = c.__dict__.get("__init__")
        if init is None or not inspect.isfunction(init):
            continue

        def make(orig):
            def __init__(self, *a, **k):
                s = cur_classes
                if s is not None:
                    s.add(type(self))
                return orig(self, *a, **k)

            __init__.__wrapped__ = orig
            return __init__

        try:
            setattr(c, "__init__", make(init))
        except Exception:
            pass
    if not TRACK:
        return
    # module-level containers
    for name, mod in list(sys.modules.items()):
        if mod is None or not name.startswith("sqlglot"):
            continue
        for k, v in list(vars(mod).items()):
            if k.startswith("__"):
                continue
            if type(v) in (dict, set, frozenset):
                w = _wrap_container(v, f"{name}::{k}")
                if w is not None:
                    setattr(mod, k, w)
    # class attributes
    for c in classes:
        if issubclass(c, (enum.Enum, type)):
            continue
        for k, v in list(vars(c).items()):
            if k in SKIP_ATTRS or (k.startswith("__") and k.endswith("__")):
                continue
            if hasattr(type(v), "__get__") or inspect.isroutine(v):
                continue
            label = f"{cname(c)}.{k}"
            if type(v) in (dict, set, frozenset):
                w = _wrap_container(v, label)
                if w is not None:
                    v = w
            try:
                setattr(c, k, Attr(v, k))
            except Exception as e:  # pragma: no cover
                print("cannot track", label, e, file=sys.stderr)


def _preload():
    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:  # pragma: no cover
            print("preload failed", m.name, e, file=sys.stderr)


def pytest_configure(config):
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.addaudithook(audit)
    _be._LoaderBasics.exec_module = _exec_module
    mon.use_tool_id(TOOL, "blastmap")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.set_events(TOOL, mon.events.LINE)
    if PRELOAD:
        _preload()


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    global cur_collector
    prev = cur_collector
    cur_collector = collector.nodeid
    try:
        return (yield)
    finally:
        cur_collector = prev


def pytest_collection_finish(session):
    global import_phase
    import_phase = False
    for modname in ("tests.test_optimizer", "tests.test_executor"):
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = InlineExecutor
    _install_tracking()
    _track_dispatch()
    _snapshot()


def _track_dispatch():
    """The generator caches a per-class dispatch table copied from TRANSFORMS the
    first time a class is used. Reads of that table count as reads of the
    TRANSFORMS keys, for every test, not just the first one to build it."""
    if not TRACK:
        return
    gen = sys.modules.get("sqlglot.generator")
    if gen is None or not hasattr(gen, "_build_dispatch"):
        print("mapplugin: no generator dispatch to track", file=sys.stderr)
        return
    orig = gen._build_dispatch

    def _build_dispatch(cls):
        d = orig(cls)
        tr = cls.TRANSFORMS
        bid = getattr(tr, "_bid", None)
        if bid is None:
            return d
        w = TDict(d)
        w._bid = bid
        return w

    gen._build_dispatch = _build_dispatch
    gen._DISPATCH_CACHE.clear()


snap = {}


def _snapshot():
    """Sizes of every module-level container and class namespace after import,
    to report afterwards which ones tests changed (caches filled at run time)."""
    for name, mod in list(sys.modules.items()):
        if mod is None or not name.startswith("sqlglot"):
            continue
        for k, v in list(vars(mod).items()):
            if isinstance(v, (dict, set, list)):
                snap[f"{name}::{k}"] = (v, len(v))
    for c in _all_classes():
        snap[cname(c) + ".__dict__"] = (c, set(vars(c)))


def _changed_after_run():
    out = []
    for label, (obj, before) in snap.items():
        if label.endswith(".__dict__"):
            new = set(vars(obj)) - before
            if new:
                out.append(f"{label} +{sorted(new)[:5]}")
        elif len(obj) != before:
            out.append(f"{label} {before}->{len(obj)}")
    return out


def _new_rec():
    return dict(setup=set(), call=set(), teardown=set(), opens=set(), sopens=set(), classes=set(),
                sclasses=set(), reads=set(), sreads=set(), secs=0.0)


def _phase(item, name):
    global cur_lines, cur_opens, cur_classes, READS
    rec = tests.get(item.nodeid)
    if rec is None:
        rec = tests[item.nodeid] = _new_rec()
    cur_lines = rec[name]
    setup = name == "setup"
    cur_opens = rec["sopens"] if setup else rec["opens"]
    cur_classes = rec["sclasses"] if setup else rec["classes"]
    READS = set()
    mon.restart_events()
    return rec


def _end(rec, name):
    global cur_lines, cur_opens, cur_classes, READS
    r = READS
    cur_lines = cur_opens = cur_classes = READS = None
    out = rec["sreads"] if name == "setup" else rec["reads"]
    for a, b in r:
        if type(a) is int:
            out.add((a, b if b is ALL else knorm(b)))
        else:
            out.add((cname(a), b))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    global setup_class
    rec = _phase(item, "setup")
    cls = getattr(item, "cls", None)
    first = cls is not None and cls not in setup_seen
    if first:
        setup_seen[cls] = set(vars(cls))
        setup_class = cls
    try:
        return (yield)
    finally:
        setup_class = None
        _end(rec, "setup")
        if first:
            try:
                _after_class_setup(item.nodeid.rsplit("::", 1)[0], cls)
            except Exception as e:  # pragma: no cover
                print("class setup tracking failed", cls, e, file=sys.stderr)


def _after_class_setup(key, cls):
    """Data a class loads once in setUpClass lands in class attributes. Note which
    attributes each opened file feeds, and track who reads those attributes, so a
    change to one file selects the tests that use its data rather than the class."""
    import ast
    import textwrap

    new = [k for k in vars(cls) if k not in setup_seen[cls] and not k.startswith("__")]
    for k in new:
        v = vars(cls)[k]
        if hasattr(type(v), "__get__") or inspect.isroutine(v):
            continue
        setattr(cls, k, Attr(v, k))
    files = setup_lines.get(cls)
    if not files:
        return
    fn = cls.__dict__.get("setUpClass")
    fn = getattr(fn, "__func__", None)
    out = {f: None for f in files}
    class_files[key] = out
    if fn is None:
        return
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    base = fn.__code__.co_firstlineno
    stmts = []  # (first line, last line, attributes of cls it mentions)
    for st in tree.body[0].body:
        names = {
            n.attr for n in ast.walk(st)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "cls"
        }
        first_line = min([d.lineno for d in getattr(st, "decorator_list", ())] + [st.lineno])
        stmts.append((base + first_line - 1, base + st.end_lineno - 1, names))
    for f, lines in files.items():
        attrs = set()
        ok = True
        for ln in lines:
            hit = [names for a, b, names in stmts if a <= ln <= b]
            if not hit or not hit[0]:
                ok = False
                break
            attrs |= hit[0]
        if not ok:
            continue
        grew = True
        while grew:  # attributes computed from those
            grew = False
            for a, b, names in stmts:
                if names & attrs and not names <= attrs:
                    attrs |= names
                    grew = True
        if all(k in new for k in attrs):
            out[f] = sorted(attrs)


# Tests that loop over a list held in a class attribute, one independent case per
# item. Each case is also recorded on its own, as "<test id>#<index>", by running
# the test with the list cut down to that item.
UNIT_LISTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds_sqls",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch_sqls",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": "tpch_sqls",
}
UNITS = os.environ.get("MAP_UNITS", "1") == "1"


unit_fp = {}  # test id -> [digests of what sqlglot produces for item i (see unitcheck.py)]


def _record_units(item):
    """Run the test once per item of its list. Each run is recorded twice: whole
    ("<id>#<i>"), and without what runs inside the calls that turn the query text
    into trees or SQL ("<id>#<i>@x"): parse_one and transpile as the test module
    calls them, and the executor's call to the optimizer. A change that reaches an
    item only inside those calls can be settled by comparing their results with
    the digests taken here (unitcheck.py)."""
    global cur_lines, cur_opens, cur_classes, READS
    import unitcheck

    attr = UNIT_LISTS[item.nodeid]
    cls = item.cls
    case = getattr(item, "_testcase", None)
    if case is None:
        print("mapplugin: no test case for", item.nodeid, file=sys.stderr)
        return
    holder = vars(cls).get(attr)
    full = holder.value if isinstance(holder, Attr) else getattr(cls, attr)
    method = getattr(case, item.name)
    mod = sys.modules[cls.__module__]
    ex = sys.modules.get("sqlglot.executor")
    state = {}

    def windowed(real):
        def inner(*a, **k):
            global cur_lines, cur_classes, READS
            outer = (cur_lines, cur_classes, READS)
            cur_lines, cur_classes, READS = state["lines"], state["classes"], state["reads"]
            mon.restart_events()
            try:
                return real(*a, **k)
            finally:
                cur_lines, cur_classes, READS = outer
                mon.restart_events()

        return inner

    saved = (ex.optimize, mod.parse_one, mod.transpile)
    ex.optimize, mod.parse_one, mod.transpile = map(windowed, saved)
    try:
        for i, one in enumerate(list(full)):
            whole = tests[f"{item.nodeid}#{i}"] = _new_rec()
            rest = tests[f"{item.nodeid}#{i}@x"] = _new_rec()
            state.update(lines=set(), classes=set(), reads=set())
            setattr(cls, attr, Attr([one], attr))
            cache = getattr(cls, "cache", None)
            if isinstance(cache, dict):
                cache.clear()  # so the reference query is transpiled again, as on a first run
            cur_lines, cur_opens, cur_classes, READS = rest["call"], rest["opens"], rest["classes"], set()
            mon.restart_events()
            t = time.perf_counter()
            try:
                method()
            except BaseException as e:  # noqa
                print("mapplugin: unit failed", item.nodeid, i, repr(e)[:200], file=sys.stderr)
            rest["secs"] = whole["secs"] = time.perf_counter() - t
            _end(rest, "call")
            whole["call"] = rest["call"] | state["lines"]
            whole["classes"] = rest["classes"] | state["classes"]
            whole["opens"] = set(rest["opens"])
            whole["reads"] = set(rest["reads"])
            READS = state["reads"]
            _end(whole, "call")
    finally:
        ex.optimize, mod.parse_one, mod.transpile = saved
        setattr(cls, attr, holder if isinstance(holder, Attr) else full)
    # the reference digests, taken the same way the selector will take them
    if item.nodeid in unitcheck.SPECS:
        try:
            kind, items = unitcheck.prepare(mod, sys.modules["tests.helpers"], item.nodeid)
            unit_fp[item.nodeid] = [unitcheck.item_digests(kind, mod, it) for it in items]
        except BaseException as e:  # noqa
            print("mapplugin: no digests for", item.nodeid, repr(e)[:200], file=sys.stderr)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    rec = _phase(item, "call")
    t = time.perf_counter()
    try:
        return (yield)
    finally:
        rec["secs"] = time.perf_counter() - t
        _end(rec, "call")
        if UNITS and item.nodeid in UNIT_LISTS:
            _record_units(item)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item):
    rec = _phase(item, "teardown")
    try:
        return (yield)
    finally:
        _end(rec, "teardown")


def pytest_sessionfinish(session):
    mon.set_events(TOOL, 0)
    out = {}
    for nodeid, rec in tests.items():
        out[nodeid] = dict(
            setup=sorted(rec["setup"]),
            call=sorted(rec["call"]),
            teardown=sorted(rec["teardown"]),
            opens=sorted(rec["opens"]),
            sopens=sorted(rec["sopens"]),
            classes=sorted(cname(c) for c in rec["classes"]),
            sclasses=sorted(cname(c) for c in rec["sclasses"]),
            reads=sorted(rec["reads"], key=repr),
            sreads=sorted(rec["sreads"], key=repr),
            secs=rec["secs"],
        )
    data = dict(
        tests=out,
        collect_opens={k: sorted(v) for k, v in collect_opens.items()},
        import_lines={k: sorted(v) for k, v in import_lines.items()},
        bags=bags,
        class_info=class_info,
        module_order=[m for m in sys.modules if m.startswith(("sqlglot", "tests"))],
        runtime_changes=_changed_after_run(),
        class_files=class_files,
        unit_lists=UNIT_LISTS if UNITS else {},
        unit_fp=unit_fp,
    )
    with open(OUT, "wb") as f:
        pickle.dump(data, f)
    print(f"mapplugin: wrote {len(out)} tests to {OUT}", file=sys.stderr)
