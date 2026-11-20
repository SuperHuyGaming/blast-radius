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
OUT = os.environ.get("MAP_OUT", "/out")
cov = coverage.Coverage(data_file=None, branch=False,
                        source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")])
current = ["<import>"]
opens: dict = {}
outcomes: dict = {}
order: list = []


def _set(ctx):
    current[0] = ctx
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
