"""pytest plugin: records, per test, which sqlglot class-body and module-level values were read.

Load with `-p clsrec` (this folder on PYTHONPATH) and CLSREC_OUT=<dir>. It must be imported before
sqlglot: it wraps builtins.__build_class__ so every sqlglot class is instrumented right after its
creation, and records reads made while later classes and modules are created (derivations).

Recorded kinds per value id i:
  R i          the whole value was used (scalar/list read, set used other than `x in S`)
  (i,'k',key)  membership test of key
  (i,'v',key)  value of key read (get/[]/pop)
  (i,'K',None) all keys read (iter/len/keys)
  (i,'V',None) all values read (items/values/copy/spread/eq)
"""

import builtins
import dis
import enum
import importlib
import importlib.abc
import importlib.machinery
import json
import os
import pickle
import sys
import types

OUT = os.environ.get("CLSREC_OUT", "/tmp/clsrec")

IMP = True  # import phase: records carry a context
R = set()
KK = set()
O = set()
F = set()
IMPREC = {}  # ctx -> set of records
DESCS = []  # id -> [module, qualname, attr, vtype]
BSTACK = []  # (body code, qualname, module)
INSTRUMENTED = set()
CONTAINS_SITES = {}


def _ctx():
    f = sys._getframe(3)
    top = BSTACK[-1] if BSTACK else None
    while f is not None:
        c = f.f_code
        if top is not None and c is top[0]:
            return ("body", top[2], top[1], f.f_lineno)
        if c is _BC_CODE:
            return ("post", top[2], top[1]) if top else ("?",)
        if c.co_name == "<module>":
            name = f.f_globals.get("__name__", "")
            if name.startswith("sqlglot"):
                return ("mod", name, f.f_lineno)
            return ("?", name)
        f = f.f_back
    return ("?",)


def _imp(rec):
    ctx = _ctx()
    s = IMPREC.get(ctx)
    if s is None:
        s = IMPREC[ctx] = set()
    s.add(rec)


def _rk(i, kind, key):
    try:
        t = (i, kind, key)
        hash(t)
    except TypeError:
        t = (i, "V", None)
    if IMP:
        _imp(t)
    else:
        KK.add(t)


def _contains_sites(code):
    s = CONTAINS_SITES.get(code)
    if s is None:
        s = set()
        prev = None
        for ins in dis.get_instructions(code):
            if ins.opname == "CONTAINS_OP" and prev is not None:
                s.add(prev.offset)
            prev = ins
        CONTAINS_SITES[code] = s
    return s


class D:
    """Non-data descriptor holding a class attribute's value; records each read."""

    __slots__ = ("v", "i", "s")

    def __init__(self, v, i, s):
        self.v = v
        self.i = i
        self.s = s

    def __get__(self, obj, owner=None):
        if IMP:
            if self.s == 0:
                _imp(self.i)
            elif self.s == 2:
                f = sys._getframe(1)
                if f.f_lasti not in _contains_sites(f.f_code):
                    _imp(self.i)
            return self.v
        O.add(owner)
        s = self.s
        if s == 0:
            R.add(self.i)
        elif s == 2:
            f = sys._getframe(1)
            if f.f_lasti not in _contains_sites(f.f_code):
                R.add(self.i)
        return self.v


class RD(dict):
    __slots__ = ("_i",)

    def __getitem__(self, k):
        _rk(self._i, "v", k)
        return dict.__getitem__(self, k)

    def get(self, k, d=None):
        _rk(self._i, "v", k)
        return dict.get(self, k, d)

    def __contains__(self, k):
        _rk(self._i, "k", k)
        return dict.__contains__(self, k)

    def pop(self, k, *d):
        _rk(self._i, "v", k)
        return dict.pop(self, k, *d)

    def setdefault(self, k, d=None):
        _rk(self._i, "v", k)
        return dict.setdefault(self, k, d)

    def __iter__(self):
        _rk(self._i, "K", None)
        return dict.__iter__(self)

    def keys(self):
        _rk(self._i, "K", None)
        return dict.keys(self)

    def __len__(self):
        _rk(self._i, "K", None)
        return dict.__len__(self)

    def values(self):
        _rk(self._i, "V", None)
        return dict.values(self)

    def items(self):
        _rk(self._i, "V", None)
        return dict.items(self)

    def copy(self):
        _rk(self._i, "V", None)
        return dict(dict.items(self))

    def __eq__(self, o):
        _rk(self._i, "V", None)
        return dict.__eq__(self, o)

    def __ne__(self, o):
        _rk(self._i, "V", None)
        return dict.__ne__(self, o)

    __hash__ = None

    def __or__(self, o):
        _rk(self._i, "V", None)
        return dict.__or__(dict(dict.items(self)), o)

    def __ror__(self, o):
        _rk(self._i, "V", None)
        r = dict(o)
        r.update(dict.items(self))
        return r

    def __repr__(self):
        _rk(self._i, "V", None)
        return dict.__repr__(self)

    def popitem(self):
        _rk(self._i, "V", None)
        return dict.popitem(self)

    def __reduce_ex__(self, proto):
        _rk(self._i, "V", None)
        return (dict, (dict(dict.items(self)),))

    def __reversed__(self):
        _rk(self._i, "K", None)
        return dict.__reversed__(self)


class RS(set):
    __slots__ = ("_i",)

    def __contains__(self, k):
        _rk(self._i, "k", k)
        return set.__contains__(self, k)

    def __iter__(self):
        _rk(self._i, "K", None)
        return set.__iter__(self)

    def __len__(self):
        _rk(self._i, "K", None)
        return set.__len__(self)

    def __reduce_ex__(self, proto):
        _rk(self._i, "K", None)
        return (set, (list(set.__iter__(self)),))

    def __repr__(self):
        _rk(self._i, "K", None)
        return set.__repr__(self)


def _new_id(module, qual, attr, vt):
    DESCS.append([module, qual, attr, vt])
    return len(DESCS) - 1


def _rd(v, i):
    d = RD(v)
    d._i = i
    return d


def _rs(v, i):
    s = RS(v)
    s._i = i
    return s


SKIP_TYPES = (types.FunctionType, staticmethod, classmethod, property, type, types.ModuleType)


def _probe_refs():
    class P:
        X = {1: 2}

    for k, v in list(P.__dict__.items()):
        if k == "X":
            return sys.getrefcount(v)


def _instrument(cls):
    if cls in INSTRUMENTED:
        return
    INSTRUMENTED.add(cls)
    if issubclass(cls, enum.Enum) or not cls.__module__.startswith("sqlglot"):
        return
    mod = cls.__module__
    qual = cls.__qualname__
    for k, v in list(cls.__dict__.items()):
        if k.startswith("__") and k.endswith("__"):
            continue
        if isinstance(v, (D,) + SKIP_TYPES) or hasattr(type(v), "__get__"):
            continue
        tv = type(v)
        if isinstance(v, (RD, RS)):
            s = 1 if isinstance(v, RD) else 2
            if DESCS[v._i][:3] == [mod, qual, k]:
                d = D(v, v._i, s)
            else:
                d = D(v, _new_id(mod, qual, k, "alias:%d" % v._i), s)
        elif tv is dict and sys.getrefcount(v) <= CLS_REFS:
            i = _new_id(mod, qual, k, "D")
            d = D(_rd(v, i), i, 1)
        elif tv is set and sys.getrefcount(v) <= CLS_REFS:
            i = _new_id(mod, qual, k, "S")
            d = D(_rs(v, i), i, 2)
        else:
            i = _new_id(mod, qual, k, "A" if tv not in (dict, set) else "A-alias")
            d = D(v, i, 0)
        type.__setattr__(cls, k, d)


def _wrap_module(m):
    name = m.__name__
    g = m.__dict__
    for k in list(g):
        v = g[k]
        if type(v) is dict and not k.startswith("__") and sys.getrefcount(v) <= MOD_REFS:
            i = _new_id(name, None, k, "D")
            g[k] = _rd(v, i)
        elif type(v) is set and not k.startswith("__") and sys.getrefcount(v) <= MOD_REFS:
            # sets at module level: C-level set ops bypass RS; still record `in` and iteration
            i = _new_id(name, None, k, "S")
            g[k] = _rs(v, i)


def _probe_mod_refs():
    m = types.ModuleType("probe")
    m.__dict__["X"] = {1: 2}
    g = m.__dict__
    for k in list(g):
        v = g[k]
        if k == "X":
            return sys.getrefcount(v)


def _wrap_ns(ns, qual, mod):
    """Swap dict/set values of a class body namespace for recording containers before the
    metaclass and __init_subclass__ see them, so their reads of the class's own attributes
    are recorded too."""
    for k, v in list(ns.items()):
        if k.startswith("__") and k.endswith("__"):
            continue
        tv = type(v)
        if tv is dict and sys.getrefcount(v) <= NS_REFS:
            ns[k] = _rd(v, _new_id(mod, qual, k, "D"))
        elif tv is set and sys.getrefcount(v) <= NS_REFS:
            ns[k] = _rs(v, _new_id(mod, qual, k, "S"))


def _meta_of(bases, kw):
    meta = kw.get("metaclass")
    if meta is None:
        meta = type(bases[0]) if bases else type
    if not isinstance(meta, type):
        return None
    for b in bases:
        bt = type(b)
        if issubclass(bt, meta):
            meta = bt
        elif not issubclass(meta, bt):
            return None
    for m in meta.__mro__:
        if m is not type and "__prepare__" in vars(m):
            return None
    return meta


def _bc(func, name, *bases, **kw):
    mod = func.__globals__.get("__name__", "")
    if not mod.startswith("sqlglot"):
        return _ORIG_BC(func, name, *bases, **kw)
    qual = func.__code__.co_qualname
    BSTACK.append((func.__code__, qual, mod))
    try:
        meta = None
        if types.resolve_bases(bases) is bases:
            meta = _meta_of(bases, kw)
        if meta is not None:

            def hookmeta(n, b, ns, **kw2):
                _wrap_ns(ns, qual, mod)
                return meta(n, b, ns, **kw2)

            kw = dict(kw)
            kw["metaclass"] = hookmeta
        cls = _ORIG_BC(func, name, *bases, **kw)
    finally:
        BSTACK.pop()
    if isinstance(cls, type):
        _instrument(cls)
    return cls


_BC_CODE = _bc.__code__
_ORIG_BC = builtins.__build_class__
CLS_REFS = _probe_refs()


def _probe_ns_refs():
    ns = {"X": {1: 2}}
    for k, v in list(ns.items()):
        return sys.getrefcount(v)


NS_REFS = _probe_ns_refs()
MOD_REFS = _probe_mod_refs()


class _Loader(importlib.abc.Loader):
    def __init__(self, inner):
        self.inner = inner

    def create_module(self, spec):
        return self.inner.create_module(spec)

    def exec_module(self, module):
        self.inner.exec_module(module)
        _wrap_module(module)

    def __getattr__(self, a):
        return getattr(self.inner, a)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None:
            return spec
        if not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        spec.loader = _Loader(spec.loader)
        return spec


def _sweep():
    seen = set()
    stack = [object]
    while stack:
        c = stack.pop()
        try:
            subs = type.__subclasses__(c)
        except TypeError:
            subs = c.__subclasses__()
        for s in subs:
            if s not in seen:
                seen.add(s)
                stack.append(s)
                if getattr(s, "__module__", "").startswith("sqlglot"):
                    _instrument(s)


class SyncPool:
    """In-process stand-in for ProcessPoolExecutor so reads made by pool tasks are recorded.
    Arguments and results round-trip through pickle as they would across processes."""

    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf

        fut = cf.Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            fut.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *its, **k):
        return [self.submit(fn, *a).result() for a in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_dispatch():
    import sqlglot.generator as G
    from sqlglot import exp

    class RDisp(dict):
        __slots__ = ("_i",)

        def get(self, k, d=None):
            if self._i is not None:
                KK.add((self._i, "v", k))
            return dict.get(self, k, d)

        def __getitem__(self, k):
            if self._i is not None:
                KK.add((self._i, "v", k))
            return dict.__getitem__(self, k)

        def __contains__(self, k):
            if self._i is not None:
                KK.add((self._i, "k", k))
            return dict.__contains__(self, k)

    def _build_dispatch(cls):
        tr = cls.TRANSFORMS
        dispatch = dict(dict.items(tr))
        for attr_name in dir(cls):
            if not attr_name.endswith("_sql") or attr_name.startswith("_"):
                continue
            expr_cls = dict.get(exp.EXPR_CLASSES, attr_name[:-4])
            if expr_cls and expr_cls not in dispatch:
                dispatch[expr_cls] = getattr(cls, attr_name)
        r = RDisp(dispatch)
        r._i = tr._i if isinstance(tr, RD) else None
        if r._i is None:
            print("clsrec: TRANSFORMS of %r not recorded" % cls, file=sys.stderr)
        return r

    G._build_dispatch = _build_dispatch
    G._DISPATCH_CACHE.clear()


def _mon(code, offset):
    F.add(code)
    return sys.monitoring.DISABLE


def _mon_imp(code, offset):
    if "sqlglot" in code.co_filename:
        _imp(("F", code))


# ---- install before sqlglot is imported ----
if "sqlglot" in sys.modules:
    raise RuntimeError("clsrec must load before sqlglot")
builtins.__build_class__ = _bc
sys.meta_path.insert(0, _Finder())

import concurrent.futures  # noqa: E402

concurrent.futures.ProcessPoolExecutor = SyncPool

import pkgutil  # noqa: E402

TOOL = 4
sys.monitoring.use_tool_id(TOOL, "clsrec")
sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, _mon_imp)
sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)

import sqlglot  # noqa: E402

IMPORT_ERRORS = []
for _m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if _m.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(_m.name)
    except Exception as e:  # noqa: BLE001
        IMPORT_ERRORS.append((_m.name, repr(e)))
_sweep()
IMP = False
_patch_dispatch()

sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, _mon)
sys.monitoring.restart_events()

TESTS = []  # nodeids, index = local test idx
BUCKETS = {}  # bucket name -> {"R": {i: [t]}, ...}; buckets: "call", "setup", "collect"
INV = {"R": {}, "KK": {}, "O": {}, "F": {}}
INV_SETUP = {"R": {}, "KK": {}, "O": {}, "F": {}}
INV_COLLECT = {"R": {}, "KK": {}, "O": {}, "F": {}}
COLLECT_FILES = []


def _reset():
    R.clear()
    KK.clear()
    O.clear()
    F.clear()
    sys.monitoring.restart_events()


def _flush(inv, idx):
    for name, s in (("R", R), ("KK", KK), ("O", O), ("F", F)):
        d = inv[name]
        for x in s:
            lst = d.get(x)
            if lst is None:
                d[x] = [idx]
            elif lst[-1] != idx:
                lst.append(idx)
    _reset()


def pytest_make_collect_report(collector):
    _reset()


def pytest_collectreport(report):
    p = report.nodeid.split("::")[0]
    if p not in COLLECT_FILES:
        COLLECT_FILES.append(p)
    _flush(INV_COLLECT, COLLECT_FILES.index(p))


def pytest_runtest_logstart(nodeid, location):
    TESTS.append(nodeid)
    _reset()


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _reset()
    yield
    _flush(INV_SETUP, len(TESTS) - 1)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _reset()
    yield
    _flush(INV, len(TESTS) - 1)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _reset()
    yield
    _flush(INV_SETUP, len(TESTS) - 1)


def _kr(k):
    if isinstance(k, enum.Enum):
        return "%s.%s" % (type(k).__name__, k.name)
    if isinstance(k, str):
        return k
    if isinstance(k, type):
        return "<%s>" % k.__name__
    if isinstance(k, tuple):
        return "(" + ",".join(_kr(x) for x in k) + ")"
    if k is None or isinstance(k, (int, float, bool)):
        return repr(k)
    return "?" + type(k).__name__


def _tname(t):
    return "%s:%s" % (getattr(t, "__module__", "?"), getattr(t, "__qualname__", "?"))


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(sqlglot.__file__)))


def _cname(c):
    fn = c.co_filename
    if fn.startswith(ROOT):
        fn = fn[len(ROOT) + 1 :]
    return "%s:%s:%d" % (fn, c.co_qualname, c.co_firstlineno)


def _ser(inv):
    out = {"R": {}, "KK": [], "O": {}, "F": {}}
    for i, l in inv["R"].items():
        out["R"][str(i)] = l
    for (i, kind, key), l in inv["KK"].items():
        out["KK"].append([i, kind, None if key is None else _kr(key), l])
    for t, l in inv["O"].items():
        out["O"].setdefault(_tname(t), []).extend(l)
    for c, l in inv["F"].items():
        fn = c.co_filename
        if "sqlglot" not in fn or "/tests/" in fn:
            continue
        out["F"].setdefault(_cname(c), []).extend(l)
    return out


def _ser_imp():
    out = []
    for ctx, recs in IMPREC.items():
        rs = []
        for r in recs:
            if isinstance(r, int):
                rs.append([r])
            elif r[0] == "F":
                rs.append(["F", _cname(r[1])])
            else:
                rs.append([r[0], r[1], None if r[2] is None else _kr(r[2])])
        out.append([list(ctx), rs])
    return out


def pytest_sessionfinish(session, exitstatus):
    sys.monitoring.set_events(TOOL, 0)
    sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, None)
    os.makedirs(OUT, exist_ok=True)
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    subclasses = {}
    for c in INSTRUMENTED:
        if not c.__module__.startswith("sqlglot"):
            continue
        subclasses[_tname(c)] = [_tname(b) for b in c.__mro__[1:] if b.__module__.startswith("sqlglot")]
    comps = {}
    try:
        from sqlglot.dialects.dialect import Dialect

        for c in INSTRUMENTED:
            if isinstance(c, type) and issubclass(c, Dialect) and c.__module__.startswith("sqlglot"):
                comps[_tname(c)] = [
                    _tname(getattr(c, a))
                    for a in ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class")
                    if isinstance(getattr(c, a, None), type)
                ]
    except Exception as e:  # noqa: BLE001
        print("clsrec: comps failed %r" % e, file=sys.stderr)
    data = {
        "comps": comps,
        "tests": TESTS,
        "collect_files": COLLECT_FILES,
        "descs": DESCS,
        "call": _ser(INV),
        "setup": _ser(INV_SETUP),
        "collect": _ser(INV_COLLECT),
        "mro": subclasses,
        "import_errors": IMPORT_ERRORS,
    }
    if wid in ("main", "gw0"):
        data["imp"] = _ser_imp()
    with open(os.path.join(OUT, "rec-%s.json" % wid), "w") as f:
        json.dump(data, f)
