"""Operand outcome map: run-time side (standard library only; the map loads on first use).

What the map is (maps/infect.bin, built by build_infect.py from runs of the pinned commit's
own test suite, no patches involved): for every comparison with one operator in sqlglot/
("a < b", "x is None", ...), the operand relations that occurred when the pinned suite ran,
and in which test. A relation "signature" says, for one evaluation, whether is, is not, ==,
!=, <, <=, >, >= gave True, False or "unknown" for the two operand values. This is condition
coverage with operand relations, a map built from the pinned commit like line coverage.

How it is used: when the edit of a file changes exactly one comparison operator at a mapped
site and nothing else (other changes in the file only comments or layout), a test whose
recorded evaluations of that site give the same result under the new operator runs exactly
as on the pinned commit: same values, same control flow. Such a test cannot fail because of
this edit. infected_tests() returns the tests with at least one evaluation whose result
differs (or is unknown), and whether such an evaluation happened in a phase that is not
attributed to single tests (module import, collection, between tests).
"""
from __future__ import annotations

import ast
import marshal
import re
import zlib
from array import array

OPS = ("Is", "IsNot", "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "In", "NotIn")
OP_INDEX = {name: i for i, name in enumerate(OPS)}
_CODING_RE = re.compile(r"coding[:=]\s*[-\w.]+")

_MAP = {}  # map path -> loaded header (or None)
_FILES = {}  # (map path, file) -> {site key: (operator index, records)}
_SETS = {}  # map path -> list of test-set bytes


class Result:
    __slots__ = ("tests", "global_phase", "site", "old_op", "new_op")

    def __init__(self, tests, global_phase, site, old_op, new_op):
        self.tests = tests  # frozenset of test numbers with an infected evaluation
        self.global_phase = global_phase  # an infected evaluation outside any single test
        self.site = site
        self.old_op = old_op
        self.new_op = new_op

    def __repr__(self):
        return "Result(%d tests, global=%s, site=%s, %s->%s)" % (
            len(self.tests), self.global_phase, self.site, self.old_op, self.new_op)


def _load(map_path):
    m = _MAP.get(map_path, 0)
    if m == 0:
        try:
            with open(map_path, "rb") as f:
                m = marshal.loads(f.read())
        except (OSError, ValueError, EOFError):
            m = None
        _MAP[map_path] = m
    return m


def _file_sites(map_path, m, rel):
    k = (map_path, rel)
    s = _FILES.get(k)
    if s is None:
        blob = m["files"].get(rel)
        if blob is None:
            return None
        s = {}
        for row in marshal.loads(zlib.decompress(blob)):
            s[tuple(row[:4])] = (row[4], row[5])
        _FILES[k] = s
    return s


def _set(map_path, m, sid):
    sets = _SETS.get(map_path)
    if sets is None:
        sets = _SETS[map_path] = marshal.loads(zlib.decompress(m["sets"]))
    a = array("H")
    a.frombytes(sets[sid])
    return a


def is_infected(sig, old, new):
    """sig: 2 bits per operator (0 False, 1 True, 2 unknown); In / NotIn are never computed."""
    if old >= 8 or new >= 8:
        return True
    a = (sig >> (2 * old)) & 3
    b = (sig >> (2 * new)) & 3
    return a == 2 or b == 2 or a != b


def _blank_or_comment(lines):
    return all((not s.strip()) or s.lstrip().startswith("#") for s in lines)


def _same(a, b, out):
    """Structural equality of two ASTs (positions ignored). One-operator comparisons whose
    operator differs (operands equal) are collected in out; returns False on any other
    difference or when out gets more than one entry."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        if isinstance(a, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
            sub = []  # the operands must be identical: no difference of any kind inside them
            if not (_same(a.left, b.left, sub) and _same(a.comparators[0], b.comparators[0], sub)) or sub:
                return False
            out.append((a, type(a.ops[0]).__name__, type(b.ops[0]).__name__))
            return len(out) <= 1
        for f in a._fields:
            if not _same(getattr(a, f, None), getattr(b, f, None), out):
                return False
        return True
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if not _same(x, y, out):
                return False
        return True
    if isinstance(a, (float, complex)):
        return repr(a) == repr(b)
    return a == b


def _parse_unit(lines, start, end, class_path):
    depth = len(class_path)
    pad = start - 1 - depth
    if pad < 0:
        return None
    head = "".join(" " * k + "class " + name + ":\n" for k, name in enumerate(class_path))
    src = "\n" * pad + head + "\n".join(lines[start - 1:end]) + "\n"
    try:
        return ast.parse(src)
    except (SyntaxError, ValueError):
        return None


def single_compare_edit(old_lines, new_lines, groups, units):
    """(site key, old operator, new operator) when the whole edit of the file is one operator
    change in a one-operator comparison; () when the edit changes no code; None otherwise.
    groups: (old line, n removed, new line, n added, removed lines, added lines) per change;
    units: [start, end, class path, kind] of the module-level and class-level statements of
    the pinned file (the selector's map)."""
    hit = {}
    for g in groups:
        o, nr, nl, na, removed, added = g
        hu = []
        for ui, u in enumerate(units):
            s, e = u[0], u[1]
            if nr > 0:
                if s <= o + nr - 1 and o <= e:
                    hu.append(ui)
            elif s < o <= e:
                hu.append(ui)
        if not hu:
            if not (_blank_or_comment(added) and _blank_or_comment(removed)):
                return None
            if o <= 2 or nl <= 2:
                if any(_CODING_RE.search(x) for x in added + removed):
                    return None
            continue
        for ui in hu:
            hit.setdefault(ui, []).append(g)
    found = []
    for ui in sorted(hit):
        s, e, class_path = units[ui][0], units[ui][1], units[ui][2]
        before = inside = 0
        for g in groups:
            if g[0] + g[1] - 1 < s and g[0] < s:
                before += g[3] - g[1]
            elif g[0] <= e:
                inside += g[3] - g[1]
        a = _parse_unit(old_lines, s, e, class_path)
        b = _parse_unit(new_lines, s + before, e + before + inside, class_path)
        if a is None or b is None:
            return None
        if not _same(a, b, found):
            return None
    if not found:
        return ()  # no code change at all (comments, layout)
    if len(found) != 1:
        return None
    node, old, new = found[0]
    return (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset), old, new


def infected_tests(path, old_lines, new_lines, groups, units, map_path):
    """None: not a mapped one-operator edit (use the normal path). Otherwise a Result."""
    m = _load(map_path)
    if m is None or path not in m["files"]:
        return None
    edit = single_compare_edit(old_lines, new_lines, groups, units)
    if not edit:
        return None
    key, old, new = edit
    sites = _file_sites(map_path, m, path)
    site = sites.get(key) if sites else None
    if site is None:
        return None
    op, records = site
    oi, ni = OP_INDEX[old], OP_INDEX[new]
    if op != oi:
        return None
    tests = set()
    glob = False
    for i in range(0, len(records), 3):
        sig, sid, g = records[i], records[i + 1], records[i + 2]
        if is_infected(sig, oi, ni):
            if sid:
                tests.update(_set(map_path, m, sid))
            if g:
                glob = True
    return Result(frozenset(tests), glob, (path,) + key, old, new)
