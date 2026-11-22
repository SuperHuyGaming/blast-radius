"""Probe: how clsmap and the Analyzer classify class/module-level edits of real sqlglot files.

usage: probe_bounds.py [pristine sqlglot checkout]
Edits are in-memory; nothing runs sqlglot or its tests. Exit 1 on any FAIL.
"""

import difflib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import blastsel  # noqa: E402
import clsmap  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else "/Users/Inrpf/ws/buildathon/work/sqlglot"


def read(path):
    with open(os.path.join(SRC, path), encoding="utf-8", newline="") as f:
        return f.read()


def edit(old, anchor, repl):
    """Replace the single occurrence of anchor."""
    assert old.count(anchor) == 1, "anchor not unique: %r (%d)" % (anchor, old.count(anchor))
    return old.replace(anchor, repl)


def changed_lines(old, new):
    ol = set()
    nl = set()
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            ol.update(range(i1 + 1, i2 + 1))
            nl.update(range(j1 + 1, j2 + 1))
    return sorted(ol), sorted(nl)


def patch_of(path, old, new):
    body = difflib.unified_diff(old.splitlines(), new.splitlines(), "a/" + path, "b/" + path, lineterm="")
    return "diff --git a/%s b/%s\n" % (path, path) + "\n".join(body) + "\n"


def analyzer():
    with open(os.path.join(ROOT, "sample", "tests.json")) as f:
        tests = json.load(f)["tests"]
    maps = blastsel.Maps(os.path.join(ROOT, "maps", "cov.bin"), tests)
    return blastsel.Analyzer(maps, SRC, clsmap)


AN = analyzer()
ALL = blastsel.ALL if hasattr(blastsel, "ALL") else clsmap.ALL


def probe(path, old, new):
    ol, nl = changed_lines(old, new)
    why = clsmap.why(path, old, new, ol, nl)
    aff = clsmap.affected(path, old, new, ol, nl)
    res = AN.analyze(patch_of(path, old, new))
    return why, aff, res


def covers_module(path, res):
    """The Analyzer result is ALL or holds every test of the module bound, and is not empty."""
    if res.all:
        return True
    mb = AN.module_bound(path)
    if mb is ALL:
        return False
    return res.bits != 0 and res.bits & mb == mb


def show(res):
    return "ALL" if res.all else "%d tests" % bin(res.bits).count("1")


def case_tableau():
    path = "sqlglot/dialects/tableau.py"
    old = read(path)
    new = edit(old, "    LOG_BASE_FIRST = False\n", "    LOG_BASE_FIRST = False\n    parser.Parser.STRICT_CAST = False\n")
    why, aff, res = probe(path, old, new)
    ok = isinstance(why, str) and why.startswith("Escapes") and aff == clsmap.ALL and res.all
    return ok, "why=%s affected=%s analyzer=%s" % (why, "ALL" if aff == clsmap.ALL else type(aff).__name__, show(res))


def case_presto_alias():
    path = "sqlglot/dialects/presto.py"
    old = read(path)
    anchor = "    TIME_MAPPING = MySQL.TIME_MAPPING\n"
    new = edit(old, anchor, anchor + '    TIME_MAPPING["%W"] = "%A"\n')
    why, aff, res = probe(path, old, new)
    ok = isinstance(why, str) and why.startswith("Escapes") and aff == clsmap.ALL and res.all
    return ok, "why=%s affected=%s analyzer=%s" % (why, "ALL" if aff == clsmap.ALL else type(aff).__name__, show(res))


def case_env_key():
    path = "sqlglot/executor/env.py"
    old = read(path)
    new = edit(old, '    "AND": sql_and,\n', '    "AND_": sql_and,\n')
    why, aff, res = probe(path, old, new)
    attr = (
        isinstance(why, list)
        and len(why) == 1
        and why[0][:4] == ("attr", "sqlglot.executor.env", "", "ENV")
        and why[0][4] != clsmap.ALL
        and why[0][5] == "changed"
    )
    # the map lookup may still widen it (an "all" derivation edge of ENV raises Unbounded there)
    ok = attr and (isinstance(aff, set) or covers_module(path, res))
    return ok, "why=%s affected=%s analyzer=%s" % (why, "%d tests" % len(aff) if isinstance(aff, set) else aff, show(res))


def case_overload():
    path = "sqlglot/helper.py"
    old = read(path)
    new = edit(old, "@t.overload\ndef to_bool(value: None) -> None: ...\n", "def to_bool(value: None) -> None: ...\n")
    why, aff, res = probe(path, old, new)
    if "TW" not in clsmap._map():
        ok = why == "Unbounded: module function decorator" and aff is None and covers_module(path, res)
        rule = "no TW"
    else:
        named = isinstance(why, list) and any(c[0] == "attr" and c[3] == "to_bool" and c[4] == clsmap.ALL for c in why)
        ok = named and aff is not None and (res.all or res.bits != 0 or aff == set())
        rule = "TW"
    return ok, "map %s; why=%s analyzer=%s" % (rule, why, show(res))


def case_import_rename():
    path = "sqlglot/typing/postgres.py"
    old = read(path)
    new = edit(old, "from sqlglot import exp\n", "from sqlglot import exp_\n")
    why, aff, res = probe(path, old, new)
    ok = isinstance(why, str) and why.startswith("Unbounded: statement") and aff is None and covers_module(path, res)
    return ok, "why=%s analyzer=%s" % (why, show(res))


def case_new_global():
    path = "sqlglot/helper.py"
    old = read(path)
    anchor = "@t.overload\ndef to_bool(value: None) -> None: ...\n"
    new = edit(old, anchor, "PROBE_GLOBAL = 1\n\n\n" + anchor)
    why, aff, res = probe(path, old, new)
    ok = why == "Unbounded: module global added/removed" and aff is None and covers_module(path, res)
    return ok, "why=%s analyzer=%s" % (why, show(res))


CASES = [
    ("1 tableau: parser.Parser.STRICT_CAST in class body -> Escapes", case_tableau),
    ("2 presto: item write on aliased TIME_MAPPING -> Escapes", case_presto_alias),
    ("3 env: ENV dict key changed -> bounded attr change", case_env_key),
    ("4 helper: @t.overload removed from to_bool -> decorator rule", case_overload),
    ("5 typing/postgres: import exp -> exp_ -> module bound, not empty", case_import_rename),
    ("6 helper: new module global -> module global added/removed", case_new_global),
]


def main():
    failed = 0
    for name, fn in CASES:
        try:
            ok, detail = fn()
        except Exception as e:
            ok = False
            detail = "error %r" % (e,)
        failed += not ok
        print("%s  %s\n      %s" % ("PASS" if ok else "FAIL", name, detail))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
