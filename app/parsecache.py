"""Parse cache of the inline stage check of test_execute_tpcds / test_execute_tpch (see
build_parse_cache.py for the file, selector._inline_run for the use).

The test's _mp_execute parses every query text with parse_one(sql) in one list comprehension
before the first task runs (the pool's futures are lazy: pool.LazyFuture computes a task at its
first result() call). There the check hands out the tree that the pinned parse gives for the
text, read from the file, instead of parsing, when all of this holds:

1. Parent side (plan_ok): the commit changes only function bodies of sqlglot/*.py files (hot
   mode; memo.changed_functions finds every edit inside a function, and the modules are equal
   once every function body is blanked), and no line of a changed function (its whole span in
   the pinned text) is a line that the pinned parse of the texts runs (the file's "cover"). So
   every line that the parse of a text runs is pinned code.
2. Child side, at the first parse call of an _mp_execute frame (state_ok): the module state of
   the modules that the parse can read (the file's "mods", statefp.snapshot(only=mods), with
   the changed function bodies counted as pinned, as everywhere in the stage check) equals the
   state in which the build parsed the texts. The pinned parse leaves that state as it was
   (checked at build time), and between two calls of the comprehension only the test's own
   pinned code and the pool's submit() run (no task, no sqlglot code): every text would be
   parsed from that same state, by the same code, and give the same tree.
3. The tree is read by pickle with no sqlglot code (object.__new__ and setattr: no node class
   defines a pickle hook), and was compared at build time object by object with the parsed one.
   The trees are read in the parent, once per batch, when a plan passes condition 1; a process
   hands out each of them at most once, and a forked child knows which ones its parents handed
   out (USED); any other request reads the text's tree from its bytes anew.

Any other call (another caller, other arguments, a text not in the file, a failed check) parses
as before. BR_PARSE_CACHE=0 turns the cache off."""
import marshal
import os
import pickle
import sys
import types

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "parse_cache.bin")
COVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "parse_cache_cover.bin")
ON = os.environ.get("BR_PARSE_CACHE", "1") == "1"
VERSION = 1
DEBUG = bool(os.environ.get("BR_DEBUG"))
_DATA = []  # [the loaded file, or None]
_COVER = []  # [the loaded cover file (the lines of condition 1 only, a small file), or None]
WHY = []
STATS = [0, 0, 0, 0.0, 0.0]
TREES = {}  # text -> a tree read in the parent (plan_ok), handed out once in a process line
USED = set()  # texts whose tree of TREES this process (or the process it was forked from) handed out  # trees handed out, state checks, failed state checks (this process)


def load():
    if not _DATA:
        data = None
        try:
            if ON and os.path.exists(PATH):
                with open(PATH, "rb") as f:
                    data = marshal.load(f)
                import selector

                if data.get("version") != VERSION or data.get("header") != selector.base_state_header():
                    data = None
                else:
                    data["texts"] = pickle.loads(data.pop("blob"))
                    data["cover"] = {p: frozenset(v) for p, v in data["cover"].items()}
                    data["only"] = frozenset(data["mods"])
                    data["names"] = frozenset(data["names"])
        except Exception:  # noqa: BLE001  (no cache: every text is parsed)
            data = None
        _DATA.append(data)
    return _DATA[0]


def load_cover():
    if not _COVER:
        data = None
        try:
            if ON and os.path.exists(COVER_PATH):
                with open(COVER_PATH, "rb") as f:
                    data = marshal.load(f)
                import selector

                if data.get("version") != VERSION or data.get("header") != selector.base_state_header():
                    data = None
                else:
                    data["cover"] = {p: frozenset(v) for p, v in data["cover"].items()}
        except Exception:  # noqa: BLE001  (no cache)
            data = None
        _COVER.append(data)
    return _COVER[0]


def plan_ok(plan, root, preload=True):
    """Parent side: condition 1 of the module text (the pinned texts are read here). preload:
    read the trees now (the inline check reaches the parse of every text; the check after the
    tests usually ends at its probe, and its child reads the trees that it takes)."""
    try:
        if not ON or plan.mode != "hot" or not plan.files:
            WHY.append("mode %s" % plan.mode)
            return False
        cover = load_cover()
        if cover is None:
            WHY.append("no data")
            return False
        import memo

        for path, new in plan.files.items():
            if new is None or not (path.startswith("sqlglot/") and path.endswith(".py")):
                return False
            with open(os.path.join(root, path), encoding="utf-8", newline="") as f:
                old = f.read()
            fns = memo.changed_functions(old, new)
            if not fns:
                WHY.append("fns")
                return False
            lines = cover["cover"].get(path)
            if lines:
                for s, e in fns.values():
                    if any(ln in lines for ln in range(s, e + 1)):
                        WHY.append("covered %s %d" % (path, s))
                        return False
        if preload and not TREES:
            # read once here, before the forks: a child takes a tree without reading it
            data = load()
            if data is None or data.get("crc") != cover.get("crc"):
                WHY.append("no data")
                return False
            TREES.update((sql, pickle.loads(blob)) for sql, blob in data["texts"].items())
        return True
    except Exception as e:  # noqa: BLE001
        WHY.append(repr(e))
        return False


def state_ok(data, alias):
    """Child side: condition 2 of the module text."""
    import time
    t0 = time.process_time()
    try:
        return _state_ok(data, alias)
    finally:
        STATS[3] += time.process_time() - t0


def _state_ok(data, alias):
    STATS[1] += 1
    try:
        import statefp

        state = snapshot(alias, data["only"], data["names"])
        ok = state == data["state"]
        if not ok and os.environ.get("BR_DEBUG"):
            d = statefp.diff(data["state"], state)
            sys.stderr.write("parse cache: %d keys differ: %r\n" % (len(d), d))
    except Exception as e:  # noqa: BLE001
        if os.environ.get("BR_DEBUG"):
            sys.stderr.write("parse cache: state check %r\n" % (e,))
        ok = False
    if not ok:
        STATS[2] += 1
    return ok


def readable(key, names):
    """The state keys that the parse can read (as build_parse_cache.readable)."""
    name = key[2] if key[0] == "G" or (key[0] == "O" and len(key) == 4) else key[3]
    return name in names or (name.startswith("__") and name.endswith("__"))


def snapshot(alias, only, names):
    """statefp.snapshot(alias, only=only) without the keys that readable() leaves out (they are
    not fingerprinted at all). The build takes the state of the file with this function too."""
    import enum

    import statefp

    f = statefp.Fingerprinter(alias, None)
    out = {}
    seen = set()
    skip = statefp.SKIP_ATTRS

    def ok(name):
        return name in names or (name.startswith("__") and name.endswith("__"))

    def add_value(key, v):
        t = type(v)
        mod = getattr(t, "__module__", "") or ""
        if mod.startswith("sqlglot") and not isinstance(v, (type, enum.Enum)) and t is not types.FunctionType:
            state = f._state(v, t)
            if state:
                for name, x in state:
                    out[("O",) + key[1:] + (name,)] = f.fp(x)
                return
        out[key] = f.fp(v)

    def add_class(cls, mod=None, path=None):
        if cls in seen:
            return
        seen.add(cls)
        mod = mod or cls.__module__
        path = path or cls.__qualname__
        for name, v in list(cls.__dict__.items()):
            if name in skip:
                continue
            if isinstance(v, type) and v.__module__ == cls.__module__ and v.__qualname__.startswith(cls.__qualname__ + "."):
                add_class(v)
                continue
            if ok(name):
                add_value(("A", mod, path, name), v)
        out[("A", mod, path, "__bases__")] = f.fp(cls.__bases__)

    for mod_name, m in sorted(sys.modules.items()):
        if m is None or not (mod_name == "sqlglot" or mod_name.startswith("sqlglot.")) or mod_name not in only:
            continue
        for name, v in list(vars(m).items()):
            if name in skip:
                continue
            if isinstance(v, type):
                if v.__module__ == mod_name:
                    add_class(v)
                elif ok(name):
                    out[("G", mod_name, name)] = f.fp(v)
                continue
            if not ok(name):
                continue
            if isinstance(v, types.ModuleType):
                out[("G", mod_name, name)] = ("M", v.__name__)
                continue
            add_value(("G", mod_name, name), v)
    for c, mod, path in statefp.class_paths().values():
        if mod in only and c not in seen:
            add_class(c, mod, path)
    return out


def install(module, cls, alias):
    """Puts the cached parse_one into the test module; returns the function that takes it out
    again, or None (no cache for this check)."""
    cover = load_cover()
    if cover is None:
        return None
    import sqlglot

    real = module.__dict__.get("parse_one")
    fn = cls.__dict__.get("_mp_execute")
    if not isinstance(real, types.FunctionType) or real is not sqlglot.__dict__.get("parse_one") or not isinstance(fn, types.FunctionType):
        return None
    code = fn.__code__
    st = [None, None, None]  # the _mp_execute frame of the last state check, its outcome, the texts
    getframe = sys._getframe
    loads = pickle.loads

    def parse_one(sql, *args, **kwargs):
        if not args and not kwargs and type(sql) is str:
            frame = getframe(1)
            if frame.f_code is code:
                if st[0] is not frame:
                    # (the file is read here, at the first call: a check that ends before the
                    # parse, at its probe, does not read it; reading it runs no sqlglot code)
                    data = load()
                    ok = data is not None and data.get("crc") == cover.get("crc") and state_ok(data, alias)
                    st[0], st[1], st[2] = frame, ok, data["texts"] if ok else None
                blob = st[2].get(sql) if st[1] else None
                if blob is not None:
                    STATS[0] += 1
                    if sql not in USED:
                        tree = TREES.get(sql)
                        if tree is not None:
                            USED.add(sql)
                            return tree
                    if DEBUG:
                        import time

                        t0 = time.process_time()
                        v = loads(blob)
                        STATS[4] += time.process_time() - t0
                        return v
                    return loads(blob)
        return real(sql, *args, **kwargs)

    module.parse_one = parse_one

    def remove():
        st[0] = None
        if module.__dict__.get("parse_one") is parse_one:
            module.parse_one = real

    return remove
