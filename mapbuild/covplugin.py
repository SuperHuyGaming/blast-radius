"""pytest plugin: per-test line coverage of sqlglot, fixture files read, dialects used.

Usage: pytest -p covplugin  (with this dir on PYTHONPATH), env COVOUT=<path prefix>.
Writes <COVOUT>.json: {test_nodeid: {"lines": {file: [lines]}, "fixtures": [...], "dialects": [...]}}
and "" for lines executed outside any test (imports, collection).
"""

import builtins
import io
import json
import os
import pkgutil
import importlib
import sys

import coverage
import pytest

ROOT = os.path.abspath(os.environ.get("SQLGLOT_ROOT", os.getcwd()))
PKG = os.path.join(ROOT, "sqlglot")
FIX = os.path.join(ROOT, "tests", "fixtures")

_cov = None
_current = ""
_fixtures = {}
_dialects = {}


def _rec_open(path):
    try:
        p = os.path.abspath(os.fspath(path))
    except TypeError:
        return
    if p.startswith(ROOT) and not p.endswith((".pyc",)):
        _fixtures.setdefault(_current, set()).add(os.path.relpath(p, ROOT).replace("\\", "/"))


def pytest_configure(config):
    global _cov
    _cov = coverage.Coverage(data_file=None, source=[PKG], branch=False)
    _cov.start()

    orig_open = builtins.open
    orig_io_open = io.open

    def open_(file, *a, **k):
        _rec_open(file)
        return orig_open(file, *a, **k)

    def io_open_(file, *a, **k):
        _rec_open(file)
        return orig_io_open(file, *a, **k)

    builtins.open = open_
    io.open = io_open_

    # run process-pool work in-process so coverage sees it
    import concurrent.futures as cf

    class SyncExecutor:
        def __init__(self, *a, initializer=None, initargs=(), **k):
            if initializer is not None:
                import pickle
                initializer(*pickle.loads(pickle.dumps(initargs)))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, fn, *a, **k):
            f = cf.Future()
            import pickle
            try:
                a, k = pickle.loads(pickle.dumps((a, k)))
                f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
            except BaseException as e:
                f.set_exception(e)
            return f

        def shutdown(self, *a, **k):
            pass

    cf.ProcessPoolExecutor = SyncExecutor

    # import every sqlglot module up front so import-time lines land in context ""
    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa
            print("import fail", m.name, e, file=sys.stderr)

    from sqlglot.dialects.dialect import Dialect

    orig_init = Dialect.__init__

    def init(self, *a, **k):
        _dialects.setdefault(_current, set()).add(type(self).__module__ + "." + type(self).__qualname__)
        return orig_init(self, *a, **k)

    Dialect.__init__ = init


def _switch(ctx):
    global _current
    _current = ctx
    _cov.switch_context(ctx)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _switch("S|" + item.nodeid)
    yield
    _switch(item.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _switch(item.nodeid)
    yield
    _switch("")


def pytest_unconfigure(config):
    _cov.stop()
    data = _cov.get_data()
    out = {}
    for f in data.measured_files():
        rel = os.path.relpath(f, ROOT).replace("\\", "/")
        for ctx in data.measured_contexts():
            pass
        # per-context lines
        data.set_query_contexts(None)
    ctxs = sorted(data.measured_contexts())
    for ctx in ctxs:
        data.set_query_context(ctx)
        d = {}
        for f in data.measured_files():
            ls = data.lines(f)
            if ls:
                d[os.path.relpath(f, ROOT).replace("\\", "/")] = sorted(ls)
        out[ctx] = {"lines": d}
    for ctx, s in _fixtures.items():
        out.setdefault(ctx, {"lines": {}})["fixtures"] = sorted(s)
    for ctx, s in _dialects.items():
        out.setdefault(ctx, {"lines": {}})["dialects"] = sorted(s)
    with open(os.environ["COVOUT"] + ".json", "w", encoding="utf-8") as fh:
        json.dump(out, fh)
