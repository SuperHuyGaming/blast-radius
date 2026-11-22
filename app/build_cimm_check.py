"""Build-time check of the C pass of procmodel._immortalize (native/_cimm.c, loaded by
procmodel.py): on lists of objects (made up ones: functions with nested code objects, constants,
lambdas, classes, containers; and gc.get_objects() after sqlglot and its tests are imported), the C
pass must write the immortal value on exactly the words that the Python pass writes (every object
of the list, the code object of every function of the list, and walked from those, every nested
code object and every other constant), give the same count, and leave other objects mortal. The
Python pass of procmodel (BR_NO_CIMM path) runs on a twin of the made up objects and must give the
same count and the same words. Then the selector's own path (procmodel.install(), gc.freeze())
must take the C pass and count the same.
Exit 1 on any difference (the Dockerfile then removes the module, and the Python pass runs).
Usage: python build_cimm_check.py REPO"""
import gc
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def expected(objs):
    """The addresses that the Python pass writes, and its count, without writing."""
    ft, ct = types.FunctionType, types.CodeType
    codes = [o.__code__ for o in objs if type(o) is ft]
    keep = list(codes)
    ids = set(map(id, objs))
    n = len(objs)
    seen = set()
    while codes:
        c = codes.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        ids.add(id(c))
        n += 1
        for k in c.co_consts:
            if type(k) is ct:
                codes.append(k)
                keep.append(k)
            else:
                ids.add(id(k))
                keep.append(k)
    return ids, n, keep


def make(tag):
    """A fresh set of objects (its code objects are compiled here, so they are new ones)."""
    src = (
        "def f%s(a, b=3.5):\n"
        "    def g(x):\n"
        "        h = lambda y: (y, 'k%s', 7.25, b'z', (1, 2.5, 'q'), frozenset({1, 2}))\n"
        "        def i():\n"
        "            return [z for z in range(3)], 1e300, 'deep%s'\n"
        "        return h, i, x + 1.5\n"
        "    return g, 'outer%s', 2 ** 70\n"
        "class C%s:\n"
        "    def m(self):\n"
        "        return ('meth%s', 9.75)\n"
        "    @staticmethod\n"
        "    def s():\n"
        "        return 123456789123456789\n"
        "dup = f%s\n"
    ) % ((tag,) * 7)
    ns = {}
    exec(compile(src, "<cimm-check-%s>" % tag, "exec"), ns)
    f = ns["f" + tag]
    g, _, _ = f(1)
    h, i, _ = g(2)
    objs = [f, g, h, i, ns["C" + tag], ns["C" + tag].m, ns["dup"], [1.5, 2.5], {"a": [3.5]}, (4.5, [5.5]), object(),
            types.SimpleNamespace(a=6.5), bytearray(b"xy"), set([7.5]), f.__code__]
    return objs, ns


def main(repo):
    import procmodel

    mv = procmodel._immortal_view()
    if mv is None:
        print("cimm check: no immortal view in this interpreter", file=sys.stderr)
        return 1
    cm = procmodel._load_c()
    if cm is None:
        print("cimm check: no C module", file=sys.stderr)
        return 1
    imm = procmodel._IMM
    bad = []

    # 1. made up objects: C vs the expected words, and the Python pass on a twin
    for tag in ("a", "b", "c"):
        objs, ns = make(tag)
        twin, ns2 = make(tag + "t")
        ids, n, keep = expected(objs)
        ids2, n2, keep2 = expected(twin)
        controls = [[1.25], {"z": 1}, object(), types.SimpleNamespace()]
        before = [sys.getrefcount(o) for o in controls]
        got = cm.immortalize(list(objs), imm)
        if got != n:
            bad.append(("count", tag, got, n))
        for i in ids:
            if mv[i >> 2] != imm:
                bad.append(("word", tag, i))
        if [sys.getrefcount(o) for o in controls] != before:
            bad.append(("controls", tag))
        # the Python pass of procmodel on the twin
        saved = dict(procmodel._state)
        procmodel._state.update(mv=mv, c=None, n=0, calls=1)
        real_get = gc.get_objects
        gc.get_objects = lambda *a: list(twin)
        try:
            procmodel._immortalize()
        finally:
            gc.get_objects = real_get
            pn = procmodel._state["n"]
            procmodel._state.clear()
            procmodel._state.update(saved)
        if pn != n2 or n2 != n:
            bad.append(("python count", tag, pn, n2, n))
        for i in ids2:
            if mv[i >> 2] != imm:
                bad.append(("python word", tag, i))
        if len(ids) != len(ids2):
            bad.append(("sizes", tag, len(ids), len(ids2)))

    # 2. the pinned tree's objects: sqlglot and its test modules imported, as the selector does
    import preload

    preload.install_environment(repo)
    preload.import_sqlglot()
    import glob
    import importlib

    os.chdir(repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    for path in sorted(glob.glob(os.path.join(repo, "tests", "**", "test_*.py"), recursive=True)):
        name = os.path.relpath(path, repo)[:-3].replace("/", ".")
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001
            pass
    objs = gc.get_objects()
    ids, n, keep = expected(objs)
    got = cm.immortalize(objs, imm)
    if got != n:
        bad.append(("count real", got, n))
    miss = sum(1 for i in ids if mv[i >> 2] != imm)
    if miss:
        bad.append(("words real", miss, len(ids)))
    print("cimm check: %d objects, %d words, count %d" % (len(objs), len(ids), got), file=sys.stderr)
    del objs

    # 3. the selector's path: procmodel.install(), then gc.freeze() takes the C pass
    procmodel._state.update(installed=False, mv=None, calls=1, n=0, c=0)
    procmodel.install()
    late, late_ns = make("late")
    funcs = [o for o in late if type(o) is types.FunctionType]
    gc.freeze()
    if type(procmodel._state["c"]) is not types.ModuleType:
        bad.append(("path: no C pass", procmodel._state["c"]))
    if procmodel._state["n"] < len(late):
        bad.append(("path count", procmodel._state["n"]))
    for f in funcs:
        if mv[id(f) >> 2] != imm or mv[id(f.__code__) >> 2] != imm:
            bad.append(("path word", f.__qualname__))
    gc.unfreeze()
    # 4. audit events (last: a hook cannot be removed): the C path of _immortalize raises the same
    # events in the same order as the Python path (one for _immortalize.__code__, then one
    # "object.__getattr__" (f, "__code__") per function of the list, in list order), and the
    # check of the words raises none
    events = []
    on = [False]

    def hook(name, args):
        if on[0]:
            events.append((name, tuple(a if isinstance(a, str) else type(a).__name__ for a in args)))

    sys.addaudithook(hook)
    seqs = {}
    for mode in ("py", "c"):
        objs, ns = make("aud" + mode)
        objs = objs + objs[:3]  # repeats: one event per occurrence
        saved = dict(procmodel._state)
        procmodel._state.update(mv=mv, c=(None if mode == "py" else cm), n=0, calls=1)
        real_get = gc.get_objects
        gc.get_objects = lambda *a: list(objs)
        events.clear()
        on[0] = True
        try:
            procmodel._immortalize()
        finally:
            on[0] = False
            gc.get_objects = real_get
            seqs[mode] = (list(events), procmodel._state["n"], procmodel._state["c"] is not None)
            procmodel._state.clear()
            procmodel._state.update(saved)
    if seqs["py"][:2] != seqs["c"][:2] or not seqs["c"][2]:
        bad.append(("audit", seqs))
    print("cimm check: audit events %d, same in both paths: %s" % (len(seqs["c"][0]), seqs["py"][:2] == seqs["c"][:2]), file=sys.stderr)
    if bad:
        print("cimm check FAILED:", bad[:20], file=sys.stderr)
        return 1
    print("cimm check ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
