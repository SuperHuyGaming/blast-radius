"""What the long-lived parent process loads before it forks one child per commit."""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import sys


class _StrictLogHandler(logging.Handler):
    """pytest installs log handlers that re-raise formatting errors, so a broken
    logging call fails the test there. This handler gives a direct run the same behaviour."""

    def emit(self, record):
        record.getMessage()

    def handleError(self, record):
        raise


def _single_thread_duckdb() -> None:
    """Every duckdb connection of the tests runs on the calling thread only. A connection
    without worker threads still works in a forked child, so the parent can build the class
    fixtures once. It also avoids one thread per CPU of the host."""
    try:
        import duckdb
    except ImportError:
        return
    real = duckdb.connect
    if getattr(real, "_br", False):
        return

    def connect(*args, **kwargs):
        config = dict(kwargs.pop("config", None) or {})
        config.setdefault("threads", 1)
        return real(*args, config=config, **kwargs)

    connect._br = True
    duckdb.connect = connect


def install_environment(root: str) -> None:
    import pool

    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)
    pool.install()
    logging.getLogger().addHandler(_StrictLogHandler(level=0))


def import_sqlglot() -> None:
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if info.name.endswith("__main__"):
            continue
        importlib.import_module(info.name)


def broken_modules() -> list:
    """Imports every sqlglot module; returns the names of those that raise."""
    bad = []
    try:
        import sqlglot
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            raise
        return ["sqlglot"]
    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.", onerror=lambda name: bad.append(name)):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, KeyboardInterrupt):
                raise
            bad.append(info.name)
    return sorted(set(bad))


def import_tests(modules) -> None:
    for name in modules:
        importlib.import_module(name)
    _single_thread_duckdb()


# Test classes whose setUpClass is costly and reads nothing that a test changes.
SHARED_FIXTURES = (
    ("tests.test_optimizer", "TestOptimizer"),
    ("tests.test_executor", "TestExecutor"),
)


def build_class_fixtures(wanted) -> list:
    """Runs setUpClass once, here in the parent, for the classes in 'wanted' (a set of
    (module, class) names). The forked children inherit the result and skip the call.
    Returns the classes that were set up."""
    done = []
    for mod_name, cls_name in SHARED_FIXTURES:
        if (mod_name, cls_name) not in wanted:
            continue
        mod = sys.modules.get(mod_name)
        cls = getattr(mod, cls_name, None) if mod else None
        if cls is None:
            continue
        cls.setUpClass()
        cls.setUpClass = classmethod(lambda c: None)
        # a child must not close what it shares with the parent and with the other children
        cls.tearDownClass = classmethod(lambda c: None)
        done.append((mod_name, cls_name))
    return done
