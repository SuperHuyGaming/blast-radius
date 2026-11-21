"""Runs a list of sqlglot tests in the current process and reports each verdict."""
from __future__ import annotations

import importlib
import os
import unittest
from unittest.suite import _ErrorHolder

from reduce import STATE


class Verdicts(unittest.TestResult):
    """Records pass or fail for each test. A failing subtest ends its test at once."""

    def __init__(self, emit, stop_after_fail=False, started=None):
        super().__init__()
        self._started = started
        self.failfast = True  # makes TestCase.subTest stop the test after a failed subtest
        self._emit = emit
        self._count = 0
        self._current = None
        self._stop_after_fail = stop_after_fail
        self.class_errors = []

    def stop(self):  # failfast must not end the whole run
        pass

    def _bad(self):
        return len(self.failures) + len(self.errors) + len(self.unexpectedSuccesses)

    def startTest(self, test):
        super().startTest(test)
        self._count = self._bad()
        self._current = test
        if self._started is not None:
            self._started(test._br_number)
        keep = getattr(test, "_br_keep", None)
        if keep is None:
            STATE.reset()
        else:
            STATE.reset(keep[0], keep[1])
        STATE.in_test = True

    def stopTest(self, test):
        super().stopTest(test)
        STATE.in_test = False
        STATE.reset()
        failed = self._bad() > self._count
        self._current = None
        self._emit(test._br_number, failed)
        if failed and self._stop_after_fail:
            self.shouldStop = True

    def addError(self, test, err):
        super().addError(test, err)
        if isinstance(test, _ErrorHolder):  # setUpClass / setUpModule / tearDownClass failed
            self.class_errors.append(test.description)


def load_case(test_id: str, number: int):
    path, cls_name, meth = test_id.split("::")
    mod = importlib.import_module(path[:-3].replace("/", "."))
    case = getattr(mod, cls_name)(meth)
    case._br_number = number
    return case


def run_tests(items, emit, stop_after_fail=False, keeps=None, started=None):
    """items: list of (number, test id), already in run order with each class contiguous.

    emit(number, failed) is called once per test. keeps: {number: (keep_files, keep_tasks)}
    for the tests that run in reduced form (see reduce.py)."""
    suite = unittest.TestSuite()
    by_class = {}
    for number, test_id in items:
        try:
            case = load_case(test_id, number)
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            emit(number, True)  # the test file cannot be imported, or the test is gone
            continue
        if keeps and number in keeps:
            case._br_keep = keeps[number]
        suite.addTest(case)
        by_class.setdefault(type(case), []).append(number)
    seen = set()

    def emit_once(number, failed):
        seen.add(number)
        emit(number, failed)

    result = Verdicts(emit_once, stop_after_fail, started)
    suite.run(result)
    if result.class_errors and not result.shouldStop:
        # a class whose setUpClass failed never starts its tests: all of them fail
        for cls, numbers in by_class.items():
            for number in numbers:
                if number not in seen:
                    emit(number, True)
    return result
