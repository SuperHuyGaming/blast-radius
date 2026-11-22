"""pytest plugin: per-test line coverage contexts + per-test file opens (audit hook)."""
import os, sys, json, threading
import coverage

ROOT = os.environ["COVROOT"]
OUT = os.environ["COVOUT"]
_wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
_cov = coverage.Coverage(data_file=os.path.join(OUT, f".coverage.{_wid}"), source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")], branch=False)
_cov.start()
_cur = [""]
_opens = {}
_ctx = [""]  # coverage context stack: test id, @scope:..., @import:<module>
_edges = {}  # module -> {importer module or "@runtime:<module>"}


def _switch(ctx):
    _cov.switch_context(ctx)
    # sysmon disables each line event after its first hit; re-arm so every
    # context records every line it runs, not only lines new to the process
    if hasattr(sys, "monitoring"):
        sys.monitoring.restart_events()


def _push(ctx):
    _ctx.append(ctx)
    _switch(ctx)


def _pop():
    _ctx.pop()
    _switch(_ctx[-1])


# Lines a sqlglot module runs while it is being imported belong to every test
# that could be first to import it, not only to the one that was: record them
# under @import:<module> and the importer edge, and let build_maps spread them.
import importlib.machinery as _im

_SQ = os.path.join(ROOT, "sqlglot") + os.sep
_orig_exec = _im.SourceFileLoader.exec_module


def _caller_module():
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if fn.startswith(_SQ) and "importlib" not in fn:
            return f.f_globals.get("__name__")
        f = f.f_back
    return None


def _exec_module(self, module):
    fn = getattr(module, "__file__", None) or ""
    if not fn.startswith(_SQ):
        return _orig_exec(self, module)
    name = module.__name__
    top = next((c for c in reversed(_ctx) if c.startswith("@import:")), None)
    imp = top[len("@import:"):] if top else None
    if imp is None:
        c = _caller_module()
        imp = ("@runtime:" + c) if c else "@test"
    _edges.setdefault(name, set()).add(imp)
    _push("@import:" + name)
    try:
        return _orig_exec(self, module)
    finally:
        _pop()


_im.SourceFileLoader.exec_module = _exec_module


class SyncPool:
    """In-process ProcessPoolExecutor, so coverage sees what pool tasks run.
    Arguments and results still round-trip through pickle (serde code runs)."""

    def __init__(self, *a, initializer=None, initargs=(), **k):
        import pickle

        self._p = pickle
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf

        p = self._p
        fut = cf.Future()
        try:
            fn2, a2, k2 = p.loads(p.dumps((fn, args, kwargs)))
            fut.set_result(p.loads(p.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *its, **k):
        return iter([self.submit(fn, *a).result() for a in zip(*its)])

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


import concurrent.futures as _cf

_cf.ProcessPoolExecutor = SyncPool

def _ctx_key():
    """Current test or scope, or the test file being collected/imported."""
    sc = next((c for c in reversed(_ctx) if c.startswith("@scope:")), None)
    if sc:
        return sc
    if _cur[0]:
        return _cur[0]
    f = sys._getframe(2)
    tests_dir = os.path.join(ROOT, "tests") + os.sep
    while f is not None:
        fn = f.f_code.co_filename
        if fn.startswith(tests_dir) and fn.endswith(".py"):
            return "@file:" + os.path.relpath(fn, ROOT)
        f = f.f_back
    return None


def _is_import():
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if "importlib._bootstrap" in fn or "importlib/_bootstrap" in fn:
            return True
        f = f.f_back
    return False


def _audit(event, args):
    if event not in ("open", "os.listdir", "os.scandir"):
        return
    if not args:
        return
    p = args[0]
    if event != "open" and p is None:
        p = os.getcwd()
    if not isinstance(p, (str, bytes, os.PathLike)):
        return
    try:
        p = os.path.abspath(os.fsdecode(p))
    except Exception:
        return
    if not p.startswith(ROOT):
        return
    if "__pycache__" in p:
        return
    key = _ctx_key()
    if key is None:
        return
    rel = os.path.relpath(p, ROOT)
    if event == "open":
        how = "import" if _is_import() else "read"
        d = _opens.setdefault(key, {})
        if d.get(rel) != "read":
            d[rel] = how
    elif not _is_import():
        _opens.setdefault(key, {})[rel + "/"] = "list"


sys.addaudithook(_audit)

import pytest

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    # process-wide memo caches: empty them so every test runs the code that
    # fills them, as it would when it is first in its process
    g = sys.modules.get("sqlglot.generator")
    if g is not None and isinstance(getattr(g, "_DISPATCH_CACHE", None), dict):
        g._DISPATCH_CACHE.clear()
    _cur[0] = item.nodeid
    _push(item.nodeid)
    yield
    _cur[0] = ""
    _pop()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    # setUpClass and class/module/session fixtures serve every test in scope
    if fixturedef.scope == "function":
        yield
        return
    _push(f"@scope:{fixturedef.scope}:{request.node.nodeid}")
    try:
        yield
    finally:
        _pop()

def pytest_sessionfinish(session, exitstatus):
    _cov.stop()
    _cov.save()
    with open(os.path.join(OUT, f"opens.{_wid}.json"), "w") as f:
        json.dump(_opens, f)
    with open(os.path.join(OUT, f"edges.{_wid}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _edges.items()}, f)
