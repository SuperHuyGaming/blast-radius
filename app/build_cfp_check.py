"""Build-time check of the C fingerprint walk (native/_statefpc.c, loaded by statefp.py): the
snapshots of the pinned state (as the selector takes them: plain, with the code hashes of an
earlier one, with a code alias, for some modules only; after the test modules are imported and
a query is optimized) and the fingerprints of a set of odd objects must be the same with the C
walk and with the Python one: the same keys in the same order, the same values, the same memo.
Exit 1 on any difference (the Dockerfile then removes the module, and the Python code runs).
Usage: python build_cfp_check.py REPO"""
import functools
import enum
import collections
import os
import random
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main(repo):
    import preload

    preload.install_environment(repo)
    preload.import_sqlglot()
    import statefp

    if statefp._cfp is None:
        print("cfp check: no C module", file=sys.stderr)
        return 1

    def make(use_c, *args):
        saved = statefp._cfp
        if not use_c:
            statefp._cfp = None
        try:
            return statefp.Fingerprinter(*args)
        finally:
            statefp._cfp = saved

    def snap(use_c, *args):
        saved = statefp._cfp
        if not use_c:
            statefp._cfp = None
        try:
            return statefp.snapshot(*args)
        finally:
            statefp._cfp = saved

    def memo(f):
        m = f.fp.memo() if hasattr(f.fp, "memo") else f.memo
        if None in m.values():
            raise SystemExit("cfp check: busy entry left")
        return m

    def same(a, b, what):
        (sa, fa), (sb, fb) = a, b
        ok = (sa == sb and list(sa) == list(sb) and memo(fa) == memo(fb)
              and fa.code_memo == fb.code_memo and list(fa.code_memo) == list(fb.code_memo)
              and list(fa.objects) == list(fb.objects) and all(fa.objects[k] is fb.objects[k] for k in fa.objects)
              and fa.function_keys == fb.function_keys and not fa.busy and not fb.busy)
        if not ok:
            raise SystemExit("cfp check: %s differs" % what)
        return fa

    def tables(objects, f):
        return {k: [f.fp(v) for v in objects[k].values()] for k in objects if type(objects[k]) is dict}

    def battery(label):
        fc = same(snap(True), snap(False), label + " snapshot")
        if tables(fc.objects, make(True)) != tables(fc.objects, make(False)):
            raise SystemExit("cfp check: %s tables differ" % label)
        bc = fc.base_codes()
        hashes = list(fc.code_memo.values())
        alias = {hashes[0]: hashes[1]} if len(hashes) > 1 else {}
        for a in (None, alias):
            same(snap(True, a, bc), snap(False, a, bc), label + " base codes")
            same(snap(True, a), snap(False, a), label + " alias")
        only = {"sqlglot.optimizer.annotate_types", "sqlglot.dialects.dialect", "sqlglot.optimizer.normalize"}
        same(snap(True, None, bc, only), snap(False, None, bc, only), label + " only")
        # the parse cache's own snapshot (parsecache.snapshot: its add_value, the Fingerprinter's fp)
        import parsecache

        names = {n for m in only for n in vars(sys.modules[m])} - {"annotate_types"}
        pc = []
        for use in (True, False):
            saved = statefp._cfp
            if not use:
                statefp._cfp = None
            try:
                pc.append(parsecache.snapshot({}, only, names))
            finally:
                statefp._cfp = saved
        if pc[0] != pc[1] or list(pc[0]) != list(pc[1]):
            raise SystemExit("cfp check: %s parse cache snapshot differs" % label)
        # one class (classroute.py)
        import sqlglot.dialects.dialect as di

        for use in (True, False):
            f = make(use)
            f.objects = {}
            f.function_keys = set()
            if use:
                ca = statefp.class_state(di.Dialect, "sqlglot.dialects.dialect", "Dialect", f), f
            else:
                cb = statefp.class_state(di.Dialect, "sqlglot.dialects.dialect", "Dialect", f), f
        if ca[0] != cb[0] or list(ca[0]) != list(cb[0]) or memo(ca[1]) != memo(cb[1]):
            raise SystemExit("cfp check: class_state differs")

    battery("pinned")
    odd(statefp, make)
    meta(statefp, make, snap)
    # the state after the tests are imported and the optimizer has run
    import glob
    import importlib

    os.chdir(repo)
    for p in sorted(glob.glob("tests/test_*.py")):
        try:
            importlib.import_module(p[:-3].replace("/", "."))
        except Exception:  # noqa: BLE001
            pass
    from sqlglot import parse_one
    from sqlglot.optimizer import optimize

    optimize(parse_one("select a, sum(b) from x where b > 1 group by a"), schema={"x": {"a": "int", "b": "int"}})
    battery("tests imported")
    print("cfp check: C and Python fingerprints agree", file=sys.stderr)
    return 0


def odd(statefp, make):
    class Col(enum.Enum):
        A = 1
        B = [1, 2]

    class BadValue(enum.Enum):
        X = 1

        @property
        def value(self):
            raise ValueError("no value")

    class BadValue2(enum.Enum):
        X = 1

        @property
        def value(self):
            raise KeyError("kv")

    class BadRepr:
        def __repr__(self):
            raise RuntimeError("x")

    class Addr:
        pass

    class DD(dict):
        pass

    class LL(list):
        pass

    class SS(set):
        pass

    class TT(tuple):
        pass

    class Fake:
        @property
        def __class__(self):
            return type

    class Unh:
        __hash__ = None

        def __repr__(self):
            return "Unh()"

    def mk_empty():
        def g():
            return y  # noqa: F821

        if False:
            y = 1  # noqa: F841
        return g

    def mk_closure():
        a = [1, {2: lambda: 3}]
        b = Addr()

        def g():
            return a, b

        return g

    def mk_bad_cell():
        v = BadValue.X

        def g():
            return v

        return g

    def deep(n):
        x = [Addr()]
        for _ in range(n):
            x = [x, Addr()]
        return x

    def fkw(a, *, b=[1]):  # noqa: B006
        pass

    cyc = [1]
    cyc.append(cyc)
    dcyc = {}
    dcyc["me"] = dcyc
    dcyc[Addr] = Addr
    from sqlglot import parse_one
    import sqlglot.dialects.dialect as di
    import sqlglot.expressions as ex

    objs = [1, "s", None, 1.5, True, b"b", 1j, [1, 2], (1,), {1: 2}, {1, 2}, frozenset({3}), bytearray(b"ab"),
            [Addr(), Addr], (Addr, int), (), (int, Addr(), str), {Addr: 1, int: [Addr()]}, {Addr(): 1},
            {1: Addr(), (1, 2): [lambda: 0]}, {Addr(), 3}, frozenset({Addr}), Col.A, Col.B, Col, Addr, enum, types,
            mk_empty(), mk_closure(), fkw, Addr.__init__, str.join, len, [].append, object.__init__,
            re.compile("a+", re.I), DD(a=1), LL([1, Addr()]), SS({1}), TT((1, Addr())), functools.partial(fkw, 1),
            BadRepr(), Addr(), collections.OrderedDict(a=1), staticmethod(fkw), classmethod(fkw),
            property(fkw, None, fkw), deep(5), deep(20), cyc, dcyc, Fake(), [Unh(), Unh()], {1: Unh()},
            mk_bad_cell(), memoryview(b"xy"), [memoryview(b"xy")], range(3), slice(1, 2), ...,
            parse_one("select 1"), [parse_one("select a from b")], {"k": parse_one("x")},
            functools.wraps(fkw)(lambda: 0), type, object, functools.reduce(lambda a, _: [a], range(17), 1),
            [(1, [2, (3, [Addr()])])] * 3, BadValue2.X, [BadValue2.X], (BadValue.X,),
            ex.Expression, ex.Select, di.Dialect, list(vars(di).values())]
    for seed in range(4):
        order = list(objs)
        random.Random(seed).shuffle(order)
        got = []
        for use in (True, False):
            f = make(use)
            out = []
            for o in order:
                for d in (0, 5, 13):
                    try:
                        out.append(("ok", f.fp(o, d)))
                    except Exception as e:  # noqa: BLE001
                        out.append(("err", type(e).__name__, str(e)))
            m = f.fp.memo() if hasattr(f.fp, "memo") else f.memo
            got.append((out, m, f.code_memo, list(f.code_memo), set(f.busy) | {k for k, x in m.items() if x is None}))
        if got[0] != got[1]:
            raise SystemExit("cfp check: odd objects differ")


def meta(statefp, make, snap):
    """Objects whose type's metaclass has its own __eq__ / __hash__ (which the Python tests of
    the type run): the same results, the same calls of those methods in the same order, the same
    memo; also in a snapshot of a made up sqlglot module that holds them."""
    log = []

    class MEq(type):
        def __eq__(cls, other):
            log.append(("eq", cls.__name__, getattr(other, "__name__", "?")))
            return True

        __hash__ = type.__hash__

    class MSt(type):
        def __eq__(cls, other):
            log.append(("st", getattr(other, "__name__", "?")))
            return other is staticmethod or other is list or other is dict

        __hash__ = type.__hash__

    class MStr(type):
        def __hash__(cls):
            log.append(("hs", cls.__name__))
            return hash(str)

        def __eq__(cls, other):
            log.append(("es", getattr(other, "__name__", "?")))
            return other is str or other is cls

    class MDict(type):
        def __hash__(cls):
            log.append(("hd", cls.__name__))
            return hash(dict)

        def __eq__(cls, other):
            log.append(("ed", getattr(other, "__name__", "?")))
            return other is dict or other is cls

    class MUnh(type):
        def __eq__(cls, other):
            return cls is other

    class MRaise(type):
        def __eq__(cls, other):
            log.append(("raise",))
            raise ValueError("eq")

        __hash__ = type.__hash__

    class MCount(type):
        def __eq__(cls, other):
            log.append(("cnt",))
            return NotImplemented

        def __hash__(cls):
            log.append(("hc",))
            return type.__hash__(cls)

    class A(metaclass=MEq):
        pass

    class S(metaclass=MSt):
        __func__ = [1, 2]

    class HS(metaclass=MStr):
        def __repr__(self):
            return "HS"

    class HD(metaclass=MDict):
        pass

    class U(metaclass=MUnh):
        pass

    class Rz(metaclass=MRaise):
        pass

    class C(metaclass=MCount):
        pass

    class LL(list, metaclass=MSt):
        pass

    class DD(dict, metaclass=MDict):
        pass

    class TT(tuple, metaclass=MCount):
        pass

    def fdef(a=TT((1, 2)), b=HS()):
        return a

    def cell(x):
        def g():
            return x

        return g

    hs, c, rz = HS(), C(), Rz()
    objs = [A(), [A(), object()], S(), {"s": S()}, hs, [hs, hs, 1], {hs: 1, "k": hs}, HD(), [HD()], U(), [U()],
            {"u": U()}, rz, [rz], c, [c, C(), (c,)], {c: c}, {C(), 1}, LL([1, 2, A()]), LL(), DD(a=1), DD({1: [2]}),
            TT((1, c)), (A, S, HS), (C,), frozenset({c}), fdef, cell(hs), cell(rz), cell(c), staticmethod(c),
            property(fdef, c), [cell(hs), hs], A, HS, MEq, functools.partial(fdef, hs), types.MethodType(fdef, c),
            {A: 1, S: 2}, {(hs, 1): c}, [[[hs]]], (hs, (c, (U(),)))]
    for seed in range(4):
        order = list(objs)
        random.Random(seed).shuffle(order)
        got = []
        for use in (True, False):
            del log[:]
            f = make(use)
            out = []
            for o in order:
                for d in (0, 13):
                    try:
                        out.append(("ok", f.fp(o, d)))
                    except Exception as e:  # noqa: BLE001
                        out.append(("err", type(e).__name__, str(e)))
            m = f.fp.memo() if hasattr(f.fp, "memo") else f.memo
            got.append((out, list(log), m, f.code_memo))
        if got[0] != got[1]:
            raise SystemExit("cfp check: metaclass objects differ")
    good = []
    for o in objs:
        try:
            make(False).fp(o)
            good.append(o)
        except Exception:  # noqa: BLE001
            pass
    for seed in range(2):
        order = list(good)
        random.Random(seed).shuffle(order)
        got = []
        for use in (True, False):
            del log[:]
            m = types.ModuleType("sqlglot._cfp_probe")
            holder = type("Holder", (), {})
            holder.__module__ = m.__name__
            for i, o in enumerate(order):
                setattr(m, "v%d" % i, o)
                setattr(holder, "a%d" % i, o)
            m.Holder = holder
            sys.modules[m.__name__] = m
            try:
                out, f = snap(use, None, None, {m.__name__})
            finally:
                del sys.modules[m.__name__]
            got.append((list(out.items()), list(log), f.function_keys, list(f.objects)))
        if got[0] != got[1]:
            raise SystemExit("cfp check: snapshot of metaclass objects differs")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
