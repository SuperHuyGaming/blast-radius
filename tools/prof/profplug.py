"""pytest plugin of the recording run (-p profplug): gives sitecustomize the test or scope
that is running, and marks the tests that ran their call phase."""

import sys

import pytest
import sitecustomize as rec


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    # the generator's dispatch table is built once a process: build it in every test, so the
    # code that fills it belongs to each test that uses it (as tools/covplug.py does)
    g = sys.modules.get("sqlglot.generator")
    if g is not None and isinstance(getattr(g, "_DISPATCH_CACHE", None), dict):
        g._DISPATCH_CACHE.clear()
    rec.set_ctx(item.nodeid)
    try:
        yield
    finally:
        rec.flush(item.nodeid)
        rec.set_ctx(None)


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    # setUpClass and class/module/session fixtures serve every test in their scope
    if fixturedef.scope == "function":
        yield
        return
    was = rec.get_ctx()
    ctx = "scope:" + request.node.nodeid
    rec.set_ctx(ctx)
    try:
        yield
    finally:
        rec.flush(ctx)
        rec.set_ctx(was)


def pytest_runtest_logreport(report):
    if report.when == "call" and not report.skipped:
        rec._write("C " + report.nodeid)
