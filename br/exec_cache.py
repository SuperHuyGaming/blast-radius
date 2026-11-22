"""Build time: parse the TPC-H/TPC-DS CSV fixtures exactly as TestExecutor.setUpClass does
(by running it on the pinned tree) and save the row lists. At run time a child reuses them
when the patch touches neither the test module, its helpers nor those fixtures.

Usage (cwd = sqlglot tree): python exec_cache.py OUT.pkl
"""

import os
import pickle
import sys

GUARD = ("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-h/", "tests/fixtures/optimizer/tpc-ds/")
CLASS = "tests/test_executor.py::TestExecutor"


def guard_ok(paths):
    return not any(p == g or (g.endswith("/") and p.startswith(g)) for p in paths for g in GUARD)


def install(mod, rows):
    """Make mod.TestExecutor.setUpClass skip CSV parsing and use the saved rows."""
    C = mod.TestExecutor
    orig = C.__dict__["setUpClass"].__func__
    real_csv = mod.csv

    class _NoRows:
        @staticmethod
        def reader(*a, **k):
            return iter([[]])  # header only: the parse loop runs zero times

    real_duckdb = mod.duckdb

    class _LazyConn:
        """Defers connecting and the CREATE VIEW statements until a query needs them."""

        def __init__(self):
            self._pending = []
            self._real = None

        def _get(self):
            if self._real is None:
                self._real = real_duckdb.connect()
                for sql in self._pending:
                    self._real.execute(sql)
                self._pending = None
            return self._real

        def execute(self, sql, *a, **k):
            if self._real is None and not a and not k and sql.lstrip().upper().startswith("CREATE VIEW"):
                self._pending.append(sql)
                return self
            return self._get().execute(sql, *a, **k)

        def close(self):
            if self._real is not None:
                self._real.close()

        def __getattr__(self, name):
            return getattr(self._get(), name)

    class _Duck:
        def __getattr__(self, name):
            return getattr(real_duckdb, name)

        @staticmethod
        def connect(*a, **k):
            return _LazyConn() if not a and not k else real_duckdb.connect(*a, **k)

    def setUpClass(cls):
        mod.csv = _NoRows
        mod.duckdb = _Duck()
        try:
            orig(cls)
        finally:
            mod.csv = real_csv
            mod.duckdb = real_duckdb
        for key, tables, schema in (
            ("tpch", cls.tpch_tables, mod.TPCH_SCHEMA),
            ("tpcds", cls.tpcds_tables, mod.TPCDS_SCHEMA),
        ):
            for name, columns in schema.items():
                tables[name] = mod.Table(columns=columns, rows=rows[key][name])

    C.setUpClass = classmethod(setUpClass)


def main():
    sys.path.insert(0, os.getcwd())
    import tests.test_executor as te

    C = te.TestExecutor
    C.setUpClass()
    rows = {
        "tpch": {k: v.rows for k, v in C.tpch_tables.items()},
        "tpcds": {k: v.rows for k, v in C.tpcds_tables.items()},
    }
    C.tearDownClass()
    with open(sys.argv[1], "wb") as fh:
        pickle.dump(rows, fh, protocol=pickle.HIGHEST_PROTOCOL)
    print("exec rows", sum(len(v) for d in rows.values() for v in d.values()))


if __name__ == "__main__":
    main()
