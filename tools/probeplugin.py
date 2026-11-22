"""pytest plugin: for every single-operator comparison in sqlglot, record at the
pinned commit which tests ever see its sibling operator give a different answer.

    is      vs ==        is not  vs !=
    <       vs <=        >       vs >=

sqlglot modules are compiled with each such comparison replaced by a call that
computes the original result (returned unchanged) and also evaluates the
sibling, noting the running test whenever the two disagree.  A patch that only
swaps one of these operators can change what a test sees only if the two
operators disagreed at that position during that test; where they never
disagree for anyone, the swap changes nothing.

This is a value profile of the code, like line coverage: no patch is involved.

    SKIP_INTEGRATION=0 PROBE_OUT=/tmp/probe PYTHONPATH=<tools>:. python -m pytest tests -p probeplugin -n 8
"""

import ast
import concurrent.futures as cf
import importlib.abc
import importlib.machinery
import marshal
import operator
import os
import pickle
import sys

import pytest

REPO = os.getcwd()
OUT = os.environ.get("PROBE_OUT", "/tmp/probe")

EXECUTED, DIFFERS_OUTSIDE_TESTS = 1, 2
OPS = {
    "Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne,
    "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
}
SIBLING = {
    "Is": operator.eq, "Eq": operator.is_, "IsNot": operator.ne, "NotEq": operator.is_not,
    "Lt": operator.le, "LtE": operator.lt, "Gt": operator.ge, "GtE": operator.gt,
}
NODES = []  # node id -> (relpath, line, col, op)
FUNCS = []  # node id -> (operator, sibling)
FLAGS = []  # node id -> int
CUR = None  # set of node ids that disagreed during the running test phase
by_test = {}  # test id -> set(node id)
by_class = {}  # "path::Class" -> set(node id), for setUpClass


def probe(nid, a, b):
    op, sib = FUNCS[nid]
    r = op(a, b)
    f = FLAGS[nid]
    if not f & EXECUTED:
        FLAGS[nid] = f = f | EXECUTED
    c = CUR
    if (nid in c) if c is not None else (f & DIFFERS_OUTSIDE_TESTS):
        return r  # already known to disagree here; the sibling can be costly to evaluate
    try:
        s = sib(a, b)
        same = (s is r) if (r is True or r is False) else False
    except BaseException:  # noqa: the sibling cannot even be evaluated
        same = False
    if not same:
        if c is not None:
            c.add(nid)
        else:
            FLAGS[nid] = f | DIFFERS_OUTSIDE_TESTS
    return r


class Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        if op not in OPS:
            return node
        nid = len(NODES)
        NODES.append((self.rel, node.lineno, node.col_offset, op))
        FUNCS.append((OPS[op], SIBLING[op]))
        FLAGS.append(0)
        call = ast.Call(
            func=ast.Name("__blast_probe__", ast.Load()),
            args=[ast.Constant(nid), node.left, node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)


class Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.relpath(path, REPO)
        tree = ast.parse(data, filename=path)
        tree = ast.fix_missing_locations(Rewrite(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__["__blast_probe__"] = probe
        return super().exec_module(module)

    def get_code(self, fullname):
        # never use cached bytecode: it lacks the probes
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not (spec.origin or "").endswith(".py"):
            return spec
        if not os.path.abspath(spec.origin).startswith(os.path.join(REPO, "sqlglot") + os.sep):
            return spec
        spec.loader = Loader(name, spec.origin)
        return spec


assert "sqlglot" not in sys.modules, "probeplugin must load before sqlglot"
sys.meta_path.insert(0, Finder())


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        fut = cf.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:  # noqa
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def pytest_configure(config):
    os.environ["SKIP_INTEGRATION"] = "0"


def pytest_collection_finish(session):
    for modname in ("tests.test_optimizer", "tests.test_executor"):
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = InlineExecutor


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    # class fixtures run here: what disagrees then counts for the whole class
    global CUR
    CUR = by_class.setdefault(item.nodeid.rsplit("::", 1)[0], set())
    try:
        return (yield)
    finally:
        CUR = None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    global CUR
    CUR = by_test.setdefault(item.nodeid, set())
    try:
        return (yield)
    finally:
        CUR = None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item):
    global CUR
    CUR = by_test.setdefault(item.nodeid, set())
    try:
        return (yield)
    finally:
        CUR = None


def pytest_sessionfinish(session):
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    key = [n[:3] for n in NODES]
    out = dict(
        flags={key[i]: f for i, f in enumerate(FLAGS)},
        by_test={t: sorted(key[i] for i in s) for t, s in by_test.items() if s},
        by_class={c: sorted(key[i] for i in s) for c, s in by_class.items() if s},
    )
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"probe_{wid}.bin"), "wb") as fh:
        marshal.dump(out, fh)
    print(f"probeplugin: {len(NODES)} comparisons, {sum(1 for f in FLAGS if f & EXECUTED)} executed", file=sys.stderr)
