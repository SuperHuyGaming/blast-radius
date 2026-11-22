"""Digests of what sqlglot produces for one item of a list-driven executor test.

The TPC executor tests do the same few things for every query: parse it, hand
it to sqlglot.executor.execute (which optimizes, plans and runs it), transpile
it to DuckDB SQL for the reference answer, and compare the two results.  The
calls that turn the query text into something else are pure functions of that
text:

    parse_one(sql)                      -> a tree
    optimize(tree, schema, ...)         -> a tree      (inside execute)
    transpile(sql, write="duckdb")      -> SQL text

`item_digests` makes those calls exactly as the test does and returns a digest
of each result.  When a patch does not touch anything an item runs outside
those calls, and all digests equal the ones taken at the pinned commit, the
rest of the item runs unchanged code on unchanged input, so its outcome is
unchanged.

When that is not enough (the change reaches the executor itself, or the
optimizer now returns a different tree), `result_digest` runs the query through
sqlglot's executor on the test's own data, exactly as the test's worker
process does, and digests the table that comes back.  The test compares that
table with what DuckDB returns for the transpiled SQL; with the same table and
the same transpiled SQL the comparison has the same outcome as at the pinned
commit.  DuckDB itself is not needed for this.

Used by tools/unit_digests.py (to take the reference digests) and by runner.py
(to compare).
"""

import hashlib
import pickle

from blastkeys import fingerprint

# test id -> (kind, schema name in tests.helpers, fixture path)
SPECS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("execute", "TPCDS_SCHEMA", "optimizer/tpc-ds/tpc-ds.sql"),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("execute", "TPCH_SCHEMA", "optimizer/tpc-h/tpc-h.sql"),
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": ("optimized", "TPCH_SCHEMA", "optimizer/tpc-h/tpc-h.sql"),
}


class Stop(BaseException):
    pass


def _text(strings):
    return hashlib.md5("\0".join(strings).encode("utf-8", "replace")).hexdigest()


DIRS = {"TPCDS_SCHEMA": "DIR_TPCDS", "TPCH_SCHEMA": "DIR_TPCH"}


class Tables(dict):
    """The tables the test class loads in setUpClass, read the same way but only
    when a query asks for them (and without the DuckDB views the class also makes)."""

    def __init__(self, mod, schema, directory):
        super().__init__()
        self.mod, self.schema, self.directory = mod, schema, directory

    def __missing__(self, name):
        import ast
        import csv

        from sqlglot.executor.table import Table

        columns = self.schema[name]  # KeyError for a name that is not a table, as in the test
        reader = csv.reader(self.mod.open_file(f"{self.directory}{name}.csv.gz"), delimiter="|")
        rows = []
        ctypes = []
        next(reader)
        for row in reader:
            if not ctypes:
                for i, v in enumerate(row):
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        self[name] = table = Table(columns=columns, rows=rows)
        return table


def prepare(mod, helpers, test_id):
    """(kind, items) for a test, with the test module set up as its pool initializer would."""
    kind, schema_name, fixture = SPECS[test_id]
    schema = getattr(helpers, schema_name)
    items = list(helpers.load_sql_fixture_pairs(fixture))
    if kind == "execute":
        from sqlglot.executor.table import Table

        # the optimizer never looks at rows: empty tables do for its digest
        mod.initializer(schema, {name: Table(columns=cols, rows=[]) for name, cols in schema.items()})
        mod._blast_tables = Tables(mod, schema, getattr(mod, DIRS[schema_name]))
    return kind, items


def result_digest(mod, item):
    """Digest of the table sqlglot's executor returns for an item, taken the way the
    test's worker process produces it and its main process receives it."""
    meta, sql, _ = item
    tree = pickle.loads(pickle.dumps(mod.parse_one(sql)))
    empty, mod._tables = mod._tables, mod._blast_tables
    try:
        table = pickle.loads(pickle.dumps(mod.mp_execute(tree, meta)))
    finally:
        mod._tables = empty
    if table is None:
        return "-"
    rows = [[(type(c).__name__, repr(c)) for c in r] for r in table.rows]
    return _text([repr(list(table.columns)), repr(rows)])


def item_digests(kind, mod, item):
    """Digests for one item, in the order the test makes the calls."""
    import sqlglot.executor as ex

    meta, sql, expected = item
    out = []
    if kind == "execute":
        tree = mod.parse_one(sql)
        out.append(fingerprint(tree))
        if meta.get("execute"):
            real = ex.optimize
            got = []

            def optimize(*a, **k):
                got.append(fingerprint(real(*a, **k)))
                raise Stop()

            ex.optimize = optimize
            try:
                mod.mp_execute(pickle.loads(pickle.dumps(tree)), meta)  # as the pool would pass it
            except Stop:
                pass
            finally:
                ex.optimize = real
            out.append(got[0] if len(got) == 1 else "?")
            out.append(_text(mod.transpile(sql, write="duckdb")))
    else:
        out.append(_text(mod.transpile(sql, write="duckdb")))
        out.append(_text(mod.transpile(expected, write="duckdb")))
    return out


def item_same(kind, mod, item, ref, inside_only):
    """Whether an item is shown to end as it did at the pinned commit: 0 no,
    1 yes by the digests of the pure calls, 2 yes by the executor's result.

    ref: the item's reference digests. inside_only: the change reaches the item
    only inside the parse / optimize / transpile calls.  An exception here means
    the item now raises: the caller takes that as "not the same"."""
    meta, sql, _ = item
    refs = [v for v in ref if isinstance(v, str)]
    if kind == "execute":
        if not meta.get("execute"):
            # the worker returns nothing for such a query: the test only parses it
            # and ships the tree to the worker
            pickle.loads(pickle.dumps(mod.parse_one(sql)))
            return 1
        if inside_only and item_digests(kind, mod, item) == refs[:3]:
            return 1
        if len(refs) < 4 or "?" in refs[2:4]:
            return 0
        if _text(mod.transpile(sql, write="duckdb")) != refs[2]:
            return 0  # DuckDB would be asked something else
        return 2 if result_digest(mod, item) == refs[3] else 0
    return 1 if inside_only and item_digests(kind, mod, item) == refs[:2] else 0
