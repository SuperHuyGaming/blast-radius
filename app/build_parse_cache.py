"""Parse cache of the stage check of test_execute_tpcds (stages.py, selector._inline_run).

The check's test parses every TPC-H / TPC-DS query text with parse_one(sql) before its first
task runs (the pool's futures are lazy: pool.LazyFuture). On a commit that cannot change what
that parse returns, the check takes the tree from this file instead of parsing (about 0.3 s of
CPU per check). Made here, on the pinned tree, right after the import of sqlglot (as
build_base_state.py):

- "cover": {repo path: lines} that the pinned parse of the texts runs (sys.settrace).
- "mods": the sqlglot modules whose state the parse can read: those of the covered files, every
  module of sqlglot.expressions (the classes of the nodes), and the modules of the tokenizer,
  parser and dialect classes.
- "state": statefp.snapshot(only=mods) right before the parse. The parse leaves that state as it
  was (checked: the snapshot after it is equal), so every text is parsed from the same state.
- "blob": the trees, pickled without sqlglot's own reducer (Expr.__reduce__ runs serde code):
  every object of a sqlglot class is written as its class plus its __dict__ and slot values,
  and read back by object.__new__ and setattr (no sqlglot class defines __new__, __setattr__
  or __setstate__: checked here). Every tree read back is compared, object by object, with the
  parsed one (types, values, slots, shared references).

Usage: PYTHONHASHSEED=0 python build_parse_cache.py REPO OUT.bin"""
import copyreg
import io
import marshal
import os
import pickle
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

VERSION = 1
FIXTURES = ("optimizer/tpc-h/tpc-h.sql", "optimizer/tpc-ds/tpc-ds.sql")
BASE_MODS = ("sqlglot", "sqlglot.tokens", "sqlglot.parser", "sqlglot.dialects", "sqlglot.dialects.dialect",
             "sqlglot.helper", "sqlglot.errors", "sqlglot.trie", "sqlglot.time")


def _sqlglot_obj(t):
    mod = getattr(t, "__module__", "") or ""
    return mod.startswith("sqlglot") and not isinstance(t, type(type)) is False


def obj_state(obj):
    """(__dict__ or None, {slot: value}) of an object."""
    d = getattr(obj, "__dict__", None)
    slots = {}
    for klass in type(obj).__mro__:
        s = klass.__dict__.get("__slots__")
        if not s:
            continue
        if isinstance(s, str):
            s = (s,)
        for name in s:
            if name in ("__dict__", "__weakref__") or name in slots:
                continue
            try:
                slots[name] = getattr(obj, name)
            except AttributeError:
                pass
    return (dict(d) if isinstance(d, dict) and d else None), slots


class Writer(pickle.Pickler):
    def reducer_override(self, obj):
        t = type(obj)
        if isinstance(obj, type) or t.__module__ is None or not t.__module__.startswith("sqlglot"):
            return NotImplemented
        import enum

        if isinstance(obj, enum.Enum):
            return NotImplemented
        d, slots = obj_state(obj)
        return (copyreg.__newobj__, (t,), (d, slots or None))


def dumps(value):
    buf = io.BytesIO()
    Writer(buf, protocol=5).dump(value)
    return buf.getvalue()


def same(a, b, pairs=None):
    """True when b is a faithful copy of a: same types, values, object state, sharing."""
    if pairs is None:
        pairs = {}
    stack = [(a, b)]
    back = {}
    while stack:
        x, y = stack.pop()
        tx = type(x)
        if tx is not type(y):
            return False
        if tx in (str, int, float, bool, bytes, type(None), complex):
            if x != y and not (tx is float and x != x and y != y):
                return False
            continue
        if isinstance(x, type) or isinstance(x, types.FunctionType):
            if x is not y:
                return False
            continue
        import enum

        if isinstance(x, enum.Enum):
            if x is not y:
                return False
            continue
        got = pairs.get(id(x))
        if got is not None:
            if got is not y:
                return False
            continue
        if back.get(id(y)) is not None and back[id(y)] is not x:
            return False
        pairs[id(x)] = y
        back[id(y)] = x
        if tx in (list, tuple):
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif tx is dict:
            if list(x.keys()) != list(y.keys()):
                return False
            stack.extend(zip(x.keys(), y.keys()))
            stack.extend(zip(x.values(), y.values()))
        elif tx in (set, frozenset):
            if x != y:
                return False
        elif (tx.__module__ or "").startswith("sqlglot"):
            dx, sx = obj_state(x)
            dy, sy = obj_state(y)
            if (dx is None) != (dy is None) or list(sx) != list(sy):
                return False
            if dx is not None:
                stack.append((dx, dy))
            stack.extend(zip(sx.values(), sy.values()))
        else:
            return False
    return True


_DYNAMIC = frozenset(("globals", "vars", "__dict__", "eval", "exec", "__import__", "import_module", "__getattribute__"))


def read_names(ran):
    """Every name that an instruction of a line that ran uses (dis.hasname: globals, attributes,
    imports), over every code object that ran in the parse. Raises when sqlglot code that ran
    reads names dynamically (getattr and the like), where a name read is not in the code."""
    import dis

    names = set()
    for code, lines in ran.items():
        for ins in dis.get_instructions(code):
            if ins.opcode in dis.hasname:
                ln = ins.positions.lineno if ins.positions is not None else None
                if ln is None or ln in lines:
                    names.add(ins.argval)
        if code.co_filename.startswith(os.path.join(os.path.abspath(sys.argv[1]), "sqlglot", "")):
            dyn = (set(code.co_names) & _DYNAMIC) | ({"getattr", "setattr", "hasattr", "delattr"} & set(code.co_names))
            if dyn:
                raise SystemExit("parse cache: %s reads names dynamically: %r" % (code.co_qualname, sorted(dyn)))
    return names


def readable(key, names):
    """False for a state key that no code of the parse can read: a module global, or a class
    attribute, whose name no instruction that ran uses (special methods, which the interpreter
    looks up by itself, always count; so do the class bases)."""
    name = key[2] if key[0] == "G" or (key[0] == "O" and len(key) == 4) else key[3]
    return name in names or (name.startswith("__") and name.endswith("__"))


def no_hooks(classes):
    """True when no class (of the trees) defines a hook that pickle.loads would run."""
    for c in classes:
        for k in type(c).__mro__ + c.__mro__:
            if k is object or k is type:
                continue
            if (k.__module__ or "").startswith("sqlglot"):
                for name in ("__new__", "__setattr__", "__setstate__", "__getattr__", "__getattribute__", "__reduce_ex__"):
                    if name in k.__dict__:
                        return False
    return True


def texts(repo):
    sys.path.insert(0, os.path.join(repo, "tests")) if False else None
    from tests.helpers import load_sql_fixture_pairs

    out = []
    for fx in FIXTURES:
        for meta, sql, _ in load_sql_fixture_pairs(fx):
            if sql not in out:
                out.append(sql)
    return out


def main(repo, out_path):
    import preload

    preload.install_environment(repo)
    preload.import_sqlglot()
    import statefp
    import selector
    import sqlglot

    root = os.path.join(os.path.abspath(repo), "")
    sqls = texts(repo)
    parse_one = sqlglot.parse_one
    cover = {}
    ran = {}  # code object -> the lines of it that ran (every file: the stdlib code too)

    def tracer(frame, event, arg):
        code = frame.f_code
        lines = ran.get(code)
        if lines is None:
            lines = ran[code] = set()
        lines.add(frame.f_lineno)
        fn = code.co_filename
        if fn.startswith(root):
            rel = fn[len(root):]
            cover.setdefault(rel, set()).add(frame.f_lineno)
        return tracer

    trees = []
    sys.settrace(tracer)
    try:
        for sql in sqls:
            trees.append(parse_one(sql))
    finally:
        sys.settrace(None)
    mods = set(BASE_MODS)
    for m in list(sys.modules):
        if m == "sqlglot.expressions" or m.startswith("sqlglot.expressions."):
            mods.add(m)
    for rel in cover:
        if rel.startswith("sqlglot/") and rel.endswith(".py"):
            mods.add(rel[:-3].replace("/", ".").removesuffix(".__init__"))
    if any(not rel.startswith("sqlglot/") for rel in cover):
        raise SystemExit("parse cache: the parse runs code outside sqlglot/: %r" % sorted(r for r in cover if not r.startswith("sqlglot/")))
    mods = sorted(m for m in mods if m in sys.modules)
    names = read_names(ran)
    # the state of those modules, before and after a parse of every text (untraced)
    import parsecache

    full, _ = statefp.snapshot(only=set(mods))
    full = {k: v for k, v in full.items() if readable(k, names)}
    s0 = parsecache.snapshot(None, frozenset(mods), frozenset(names))
    if full != s0:
        raise SystemExit("parse cache: the key subset differs from the snapshot's")
    trees2 = [parse_one(sql) for sql in sqls]
    s1 = parsecache.snapshot(None, frozenset(mods), frozenset(names))
    if s0 != s1:
        raise SystemExit("parse cache: the parse changes module state: %r" % statefp.diff(s0, s1)[:5])
    blobs = {}
    classes = set()
    for sql, tree, tree2 in zip(sqls, trees, trees2):
        if not same(tree, tree2):
            raise SystemExit("parse cache: two parses differ")
        blob = dumps(tree)
        back = pickle.loads(blob)
        if not same(tree, back) or not same(tree2, back):
            raise SystemExit("parse cache: a tree read back differs")
        for node in tree.walk():
            classes.add(type(node))
        blobs[sql] = blob
    if not no_hooks(classes):
        raise SystemExit("parse cache: a node class has a pickle hook")
    import zlib

    cover_out = {rel: sorted(lines) for rel, lines in sorted(cover.items())}
    blob_all = pickle.dumps(blobs, protocol=5)
    crc = zlib.crc32(marshal.dumps(cover_out) + blob_all + marshal.dumps(sorted(s0.items())))
    small = {"version": VERSION, "header": selector.base_state_header(), "cover": cover_out, "crc": crc}
    data = {
        "version": VERSION,
        "crc": crc,
        "header": selector.base_state_header(),
        "cover": cover_out,
        "mods": mods,
        "names": sorted(names),
        "state": s0,
        "blob": blob_all,
    }
    with open(out_path, "wb") as fh:
        marshal.dump(data, fh)
    # the lines of condition 1 alone (parsecache.load_cover: the parent reads only this file
    # when it plans a check; the crc ties the two files together)
    with open(os.path.join(os.path.dirname(out_path), "parse_cache_cover.bin"), "wb") as fh:
        marshal.dump(small, fh)
    print("parse cache: %d texts, %d files covered, %d modules, %d state keys" % (len(blobs), len(cover), len(mods), len(s0)), file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
