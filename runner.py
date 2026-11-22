"""Running sqlglot's own tests inside the selector's process.

Used on a patched copy of sqlglot loaded in a forked child: a candidate test
that passes there cannot fail on that commit and is dropped from the selection.
"""

import concurrent.futures as cf
import contextlib
import importlib
import os
import pickle
import signal
import sys
import time
import unittest


class TestTimeout(BaseException):
    """Not an Exception, so code under test cannot swallow it."""


class InlineExecutor:
    """Stands in for the process pools two test modules create.  A spawned worker
    would import sqlglot from disk and miss the patch, so the work runs here,
    with the same pickling round trip arguments and results would go through."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        fut = cf.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
            fut.set_result(res)
        except TestTimeout:
            raise
        except BaseException as e:  # noqa
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@contextlib.contextmanager
def _sub_test(self, msg=None, **params):
    # the first failing subtest fails the test, so the rest need not run;
    # a skip inside a subtest skips only that subtest, as unittest does
    try:
        yield
    except unittest.SkipTest:
        pass


class Result(unittest.TestResult):
    def __init__(self, limit_for):
        super().__init__()
        self.failed = set()
        self.limit_for = limit_for
        self.group = ()
        self.times = {}
        self._t = 0.0
        self.armed = False
        self.done = set()
        self.keep_going = None
        self.timed_out = False

    def _key(self, test):
        return (type(test).__module__, type(test).__qualname__, test._testMethodName)

    def startTest(self, test):
        self.timed_out = False
        super().startTest(test)
        self._t = time.process_time()
        self.armed = True
        signal.setitimer(signal.ITIMER_REAL, self.limit_for(self._key(test)), 0.25)

    def stopTest(self, test):
        self.armed = False
        signal.setitimer(signal.ITIMER_REAL, 0)
        self.times[self._key(test)] = time.process_time() - self._t
        if self.timed_out:
            self.failed.add(self._key(test))  # however it ended, a test that ran out of time fails
        super().stopTest(test)
        self.done.add(self._key(test))
        if self.keep_going is not None and not self.keep_going(self):
            self.shouldStop = True

    def _bad(self, test):
        if isinstance(test, unittest.TestCase) and hasattr(test, "_testMethodName") and type(test).__name__ != "_ErrorHolder":
            self.failed.add(self._key(test))
        else:  # setUpClass / tearDownClass / module fixture failed
            self.failed.update(self.group)

    def addError(self, test, err):
        self._bad(test)

    def addFailure(self, test, err):
        self._bad(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._bad(test)

    def addUnexpectedSuccess(self, test):
        self._bad(test)


def prepare(src, test_sources, data, tmpdir, fresh=True):
    """Make the tests see the patched tree: fresh imports of every test module
    (patched ones from `test_sources`), and `open` of an edited data file
    returning its patched text.  With fresh=False the test modules already
    imported are kept: right when sqlglot was patched in place, so that every
    object they hold is still the one in use."""
    import builtins
    import io
    import linecache
    import tokenize

    import engine

    for name in [m for m in sys.modules if m == "tests" or m.startswith("tests.")] if fresh else ():
        del sys.modules[name]
    if test_sources:
        sys.meta_path.insert(0, engine.Overlay(src, test_sources))
    importlib.invalidate_caches()
    if not data:
        return
    redirect = {}
    for i, (rel, text) in enumerate(sorted(data.items())):
        full = os.path.join(src, rel)
        tmp = os.path.join(tmpdir, f"{i}_{os.path.basename(rel)}")
        with open(tmp, "wb") as f:
            f.write(text.encode("utf-8", "surrogateescape"))
        redirect[full] = tmp
        if rel.endswith(".py"):
            lines = text.splitlines(True)
            linecache.cache[full] = (len(text), None, lines, full)
    real_open = builtins.open

    def _map(file):
        if isinstance(file, (str, bytes, os.PathLike)):
            try:
                p = os.fsdecode(file)
            except Exception:
                return file
            if not os.path.isabs(p):
                p = os.path.join(os.getcwd(), p)
            return redirect.get(os.path.normpath(p), file)
        return file

    def patched_open(file, *a, **k):
        return real_open(_map(file), *a, **k)

    builtins.open = patched_open
    io.open = patched_open
    tokenize._builtin_open = patched_open


def split_id(test_id):
    path, klass, method = test_id.split("::")
    return path[:-3].replace("/", "."), klass, method


def run(src, test_ids, limit_for, patch_pools=True, class_times=None, keep_going=None):
    """Run tests by id, in the order given (grouped by class, classes in order of
    first appearance). Returns (failing ids, {id: cpu seconds}, ids run).

    keep_going(failed ids, ids run so far) is asked after every test; once it
    says no, the remaining tests are not run."""
    os.chdir(src)
    unittest.TestCase.subTest = _sub_test
    res = Result(lambda key: limit_for(by_key[key]))
    if keep_going is not None:
        res.keep_going = lambda r: keep_going(
            {by_key[k] for k in r.failed if k in by_key}, {by_key[k] for k in r.done if k in by_key}
        )
    by_key = {}
    groups = {}
    for tid in test_ids:
        mod, klass, method = split_id(tid)
        by_key[(mod, klass, method)] = tid
        groups.setdefault((mod, klass), []).append(method)

    def on_alarm(signum, frame):
        if res.armed:
            res.timed_out = True
            raise TestTimeout()

    signal.signal(signal.SIGALRM, on_alarm)
    failed = set()
    for (modname, klass), methods in groups.items():
        if res.shouldStop:
            break
        keys = [(modname, klass, m) for m in methods]
        t_class = time.process_time()
        try:
            mod = importlib.import_module(modname)
            if patch_pools and hasattr(mod, "ProcessPoolExecutor"):
                mod.ProcessPoolExecutor = InlineExecutor
            cls = getattr(mod, klass)
            cases = [cls(m) for m in methods]
        except BaseException:  # noqa: the file cannot be imported, or the test is gone
            failed.update(by_key[k] for k in keys)
            continue
        res.group = keys
        try:
            unittest.TestSuite(cases).run(res)
        except BaseException:  # noqa
            failed.update(by_key[k] for k in keys)
        finally:
            res.armed = False
            signal.setitimer(signal.ITIMER_REAL, 0)
        if class_times is not None:
            class_times[(modname, klass)] = time.process_time() - t_class
    failed.update(by_key[k] for k in res.failed if k in by_key)
    ran = {by_key[k] for k in res.done if k in by_key} | failed
    return failed, {by_key[k]: v for k, v in res.times.items() if k in by_key}, ran


def run_units(src, jobs, limit_s, patch_pools=True):
    """Run list-driven tests one item at a time.

    jobs: [(test id, class attribute holding the list, [item indices])], all of one
    class. The class fixture is set up once; each chosen item is run as the whole
    test with the list cut down to that item, stopping a test at its first failing
    item. Returns {test id: True if every chosen item passed}, or None when the
    class could not be set up (nothing is known then).
    """
    os.chdir(src)
    unittest.TestCase.subTest = _sub_test
    modname, klass, _ = split_id(jobs[0][0])
    state = {"res": None}

    def on_alarm(signum, frame):
        res = state["res"]
        if res is not None and res.armed:
            res.timed_out = True
            raise TestTimeout()

    signal.signal(signal.SIGALRM, on_alarm)
    try:
        mod = importlib.import_module(modname)
        if patch_pools and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = InlineExecutor
        cls = getattr(mod, klass)
        cls.setUpClass()
    except BaseException:  # noqa
        return None
    out = {}
    try:
        for test_id, attr, indices in jobs:
            method = split_id(test_id)[2]
            full = list(getattr(cls, attr))
            ok = True
            try:
                for i in indices:
                    if i >= len(full):
                        ok = False
                        break
                    setattr(cls, attr, [full[i]])
                    res = Result(lambda key: limit_s)
                    state["res"] = res
                    try:
                        cls(method).run(res)
                    except BaseException:  # noqa
                        ok = False
                    finally:
                        res.armed = False
                        signal.setitimer(signal.ITIMER_REAL, 0)
                    if res.failed or res.errors or res.failures:
                        ok = False
                    if not ok:
                        break
            finally:
                setattr(cls, attr, full)
            out[test_id] = ok
    finally:
        state["res"] = None
        try:
            cls.tearDownClass()
            cls.doClassCleanups()
        except BaseException:  # noqa
            pass
    return out


def items_changed(src, test_id, indices, digests, give_up=0):
    """Items of a list-driven executor test for which sqlglot no longer produces
    exactly what it produced at the pinned commit (see unitcheck.py).  None when
    that cannot be told, or when more than `give_up` differ: the caller then
    keeps the whole test.  No data is loaded and no query is executed."""
    import unitcheck

    if test_id not in unitcheck.SPECS:
        return None
    os.chdir(src)
    try:
        mod = importlib.import_module(split_id(test_id)[0])
        helpers = importlib.import_module("tests.helpers")
        kind, items = unitcheck.prepare(mod, helpers, test_id)
        changed = []
        for i in indices:
            if i >= len(items) or i >= len(digests) or not digests[i]:
                return None
            try:
                same = unitcheck.item_digests(kind, mod, items[i]) == list(digests[i])
            except Exception:
                same = False  # the item now raises before anything is executed
            if not same:
                changed.append(i)
                if len(changed) > give_up:
                    return None
        return changed
    except BaseException:  # noqa
        return None


def items_settled(src, test_id, check, run, digests, exec_cost, worth):
    """True when every listed item of a list-driven executor test is shown to end
    as it did at the pinned commit (see unitcheck.py): the test then passes.
    `check` items are reached by the change only inside the calls that parse,
    optimize and transpile the query; `run` items anywhere.  False as soon as one
    item cannot be shown unchanged; None when nothing could be told.  DuckDB is
    never asked anything.

    exec_cost(i): CPU expected for running item i through the executor.
    worth(cpu): whether the work still ahead is worth that much CPU; asked before
    every item, with the share of checked items that needed the executor so far
    applied to the ones left."""
    import unitcheck

    if test_id not in unitcheck.SPECS:
        return None
    os.chdir(src)
    try:
        mod = importlib.import_module(split_id(test_id)[0])
        helpers = importlib.import_module("tests.helpers")
        kind, items = unitcheck.prepare(mod, helpers, test_id)
        todo = [(i, True) for i in check] + [(i, False) for i in run]
        ahead = [0.0] * (len(todo) + 1)  # executor cost of the items from k on
        for k in range(len(todo) - 1, -1, -1):
            ahead[k] = ahead[k + 1] + exec_cost(todo[k][0])
        checks_left = len(check)
        seen = moved = 0
        for k, (i, inside_only) in enumerate(todo):
            if i >= len(items) or i >= len(digests) or not digests[i]:
                return False
            if inside_only:
                q = (moved + 0.3) / (seen + 2.0)
                tail = ahead[len(check)]
                left = exec_cost(-1) * checks_left + q * (ahead[k] - tail) + tail
            else:
                left = ahead[k]
            if not worth(left):
                return False
            try:
                r = unitcheck.item_same(kind, mod, items[i], digests[i], inside_only)
            except Exception:
                r = 0
            if not r:
                return False
            if inside_only:
                checks_left -= 1
                seen += 1
                moved += r == 2
        return True
    except BaseException:  # noqa
        return None
