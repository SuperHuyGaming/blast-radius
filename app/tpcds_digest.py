"""Per-query digest of what test_execute_tpcds feeds its downstream code.

python tpcds_digest.py ROOT                     -> JSON list, one sha256 per TPC-DS fixture query
python tpcds_digest.py ROOT BASELINE [OVERLAY]  -> EQUAL, or exit 3 at the first differing query

OVERLAY is a JSON {"sqlglot/x.py": "/tmp/file.py"}: those modules load from the given source.
Each digest covers the parsed tree (what the test pickles to its pool) and, for the executed
queries, the tree optimize() returns inside execute() and the DuckDB SQL of the expected side.
Trees are dumped with every slot downstream code can read, in argument order.
"""

import hashlib
import importlib.abc
import importlib.util
import json
import pickle
import sys


class Overlay(importlib.abc.MetaPathFinder):
    def __init__(self, root, mapping):
        self.by_module = {}
        for path, src in mapping.items():
            mod = path[:-3].replace("/", ".")
            pkg = mod.endswith(".__init__")
            if pkg:
                mod = mod[: -len(".__init__")]
            self.by_module[mod] = (src, root + "/" + path.rsplit("/", 1)[0] if pkg else None)

    def find_spec(self, fullname, path=None, target=None):
        hit = self.by_module.get(fullname)
        if hit is None:
            return None
        src, pkg_dir = hit
        return importlib.util.spec_from_file_location(
            fullname, src, submodule_search_locations=[pkg_dir] if pkg_dir else None)


def main(root, baseline=None, overlay=None):
    sys.path[:0] = [root]
    sys.setrecursionlimit(100000)
    if overlay:
        with open(overlay) as fh:
            sys.meta_path.insert(0, Overlay(root, json.load(fh)))
    from sqlglot import parse_one, transpile
    from sqlglot.expressions import Expr
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    want = None
    if baseline:
        with open(baseline) as fh:
            want = json.load(fh)
    got = []
    h = None

    def dump(x):
        if isinstance(x, Expr):
            h.update(b"N(" + type(x).__module__.encode() + b"." + type(x).__qualname__.encode())
            h.update(repr((x.arg_key, x.index, x.comments)).encode())
            meta = getattr(x, "_meta", None)
            h.update(repr(sorted((k, repr(v)) for k, v in meta.items()) if meta else None).encode())
            t = getattr(x, "_type", None)
            h.update(b"T")
            if t is not None:
                dump(t)
            for k, v in x.args.items():
                h.update(b"K" + k.encode())
                dump(v)
            h.update(b")")
        elif isinstance(x, list):
            h.update(b"L[")
            for i in x:
                dump(i)
            h.update(b"]")
        else:
            h.update(b"V" + type(x).__name__.encode() + repr(x).encode())

    for i, (meta, sql, _) in enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
        h = hashlib.sha256()
        e = parse_one(sql)
        h.update(b"Q")
        dump(e)
        if meta.get("execute"):
            e2 = pickle.loads(pickle.dumps(e))
            dump(optimize(e2, ensure_schema(TPCDS_SCHEMA), leave_tables_isolated=True, dialect=None))
            h.update(transpile(sql, write="duckdb")[0].encode())
        got.append(h.hexdigest())
        if want is not None and (i >= len(want) or got[i] != want[i]):
            sys.exit(3)
    if want is None:
        print(json.dumps(got))
    elif len(got) == len(want):
        print("EQUAL")
    else:
        sys.exit(3)


if __name__ == "__main__":
    main(*sys.argv[1:4])
