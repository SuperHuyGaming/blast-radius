"""Test selection for one sqlglot patch.

Everything here is stdlib and runs inside the scorer's container. The maps it
reads (maps/) are built from the pinned sqlglot commit only: per-test line
coverage, per-test file reads. Nothing is keyed by patch.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import re
import sys
import zlib
from dataclasses import dataclass, field

ALL = "ALL"  # sentinel: every test


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------
# unified diff parsing
# --------------------------------------------------------------------------

@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list = field(default_factory=list)  # (old_start, old_len, new_start, new_len, lines)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.old_path or self.new_path


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip(p: str) -> str | None:
    p = p.strip()
    if "\t" in p:
        p = p.split("\t", 1)[0]
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git (\S+) (\S+)", line)
            a = _strip(m.group(1)) if m else None
            b = _strip(m.group(2)) if m else None
            cur = FilePatch(a, b)
            files.append(cur)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch(None, None)
                files.append(cur)
            cur.old_path = _strip(line[4:])
            cur.new_path = _strip(lines[i + 1][4:])
            i += 2
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            if cur is not None:
                cur.binary = True
            i += 1
            continue
        m = _HUNK.match(line)
        if m and cur is not None:
            os_, ol, ns, nl = m.group(1), m.group(2), m.group(3), m.group(4)
            os_, ns = int(os_), int(ns)
            ol = 1 if ol is None else int(ol)
            nl = 1 if nl is None else int(nl)
            body = []
            i += 1
            co, cn = 0, 0
            while i < n and (co < ol or cn < nl):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1] if l else " "
                if tag == " ":
                    co += 1
                    cn += 1
                elif tag == "-":
                    co += 1
                elif tag == "+":
                    cn += 1
                else:
                    break
                body.append((tag, l[1:]))
                i += 1
            while i < n and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        if cur is not None and line.startswith("rename from "):
            cur.old_path = line[len("rename from "):].strip()
        elif cur is not None and line.startswith("rename to "):
            cur.new_path = line[len("rename to "):].strip()
        elif cur is not None and line.startswith("new file mode"):
            cur.old_path = None
        elif cur is not None and line.startswith("deleted file mode"):
            cur.new_path = None
        i += 1
    return files


def apply_hunks(old_text: str, hunks, blocks: list | None = None) -> tuple[str, set[int], set[int]]:
    """Returns new text, changed old line numbers (deleted/replaced), and
    insertion points (old line number after which lines were added; 0 = top).
    `blocks`, when given, collects every contiguous change as
    (deleted old line numbers, old line before the block, added new line numbers)."""
    old = old_text.split("\n")
    out: list[str] = []
    changed: set[int] = set()
    inserts: set[int] = set()
    pos = 0  # 0-based index into old
    for os_, ol, ns, nl, body in hunks:
        start = os_ - 1 if ol > 0 else os_
        if start > pos:
            out.extend(old[pos:start])
            pos = start
        oline = start  # count of old lines consumed = 1-based number of the last one
        blk = None
        for tag, text in body:
            if tag == " ":
                out.append(text)
                oline += 1
                blk = None
                continue
            if blk is None:
                blk = ([], oline, [])
                if blocks is not None:
                    blocks.append(blk)
            if tag == "-":
                changed.add(oline + 1)
                blk[0].append(oline + 1)
                oline += 1
            else:
                out.append(text)
                blk[2].append(len(out))
                if not blk[0]:
                    inserts.add(blk[1])  # pure insertion after old line blk[1]
        pos = oline
    out.extend(old[pos:])
    return "\n".join(out), changed, inserts


def trivial_lines(src: str) -> set[int]:
    """Lines holding nothing but a comment or whitespace (outside strings)."""
    import io
    import tokenize

    lines = src.split("\n")
    meaningful: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            for ln in range(tok.start[0], tok.end[0] + 1):
                meaningful.add(ln)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set()
    return {i for i in range(1, len(lines) + 1) if i not in meaningful}


# --------------------------------------------------------------------------
# maps
# --------------------------------------------------------------------------

class Maps:
    def __init__(self, path: str, tests: list[dict]):
        with open(path, "rb") as f:
            raw = json.loads(zlib.decompress(f.read()))
        self.ntests = raw["ntests"]
        ids = raw["ids"]
        # map file's test index -> scorer's test number
        num = {t["id"]: t["number"] for t in tests}
        self.remap = [num.get(i) for i in ids]
        self.bit_of_num = {n: i for i, n in enumerate(self.remap) if n is not None}
        self.sets = [int.from_bytes(base64.b64decode(s), "little") for s in raw["sets"]]
        self.files = raw["files"]  # path -> {"exec": [...], "lines": {line: setidx}}
        self.reads = raw["reads"]  # path -> setidx
        self.lists = raw.get("lists", {})
        self.test_closure = raw.get("test_closure", {})  # tests/ module -> tests importing it  # "dir/" -> setidx: tests that listed the directory
        self.need = raw.get("need", {})  # sqlglot module path -> setidx: tests that need it imported
        self.all_bits = (1 << self.ntests) - 1
        self._lines_cache: dict = {}
        # comparison sites ("path:line:col:op") where swapping is/== or is not/!= gave the
        # same bool at every evaluation in the pinned suite (tools/eqv)
        self.eqv: set[str] = set()
        eqv = os.path.join(os.path.dirname(path), "eqv.txt")
        if os.path.exists(eqv):
            with open(eqv) as f:
                for ln in f:
                    p, _, sites = ln.rstrip("\n").partition("\t")
                    if sites:
                        self.eqv.update(f"{p}:{x}" for x in sites.split())
        # per-test mutant profiles of operator sites (tools/prof), read on first use
        self._prof_path = os.path.join(os.path.dirname(path), "prof.txt")
        self._prof = None
        self._ids = ids
        self.unit_test = 0  # bit of the test split into units; 0 = none
        self.unit_all = 0  # bit above the tests: every unit of it is in play
        self.unit_count = 0

    def add_units(self, test_id: str, n: int, lines: dict, pre: dict):
        """Splits one test into n units. Unit i gets bit ntests + i on the lines
        it runs (`lines`: path -> {line: unit mask}). Every other way the test
        is reached sets `unit_all`: sets without unit detail, and lines the
        test's process runs before its units start (`pre`: path -> lines)."""
        if test_id not in self._ids:
            return
        tb = 1 << self._ids.index(test_id)
        base = self.ntests
        every = 1 << (base + n)
        self.sets = [s | every if s & tb else s for s in self.sets]
        for path, per in lines.items():
            d, _ = self.lines(path)
            early = set(pre.get(path, ()))
            for ln, mask in per.items():
                s = d.get(ln)
                bits = (self.sets[s] & ~every) if s is not None else 0
                if ln in early:
                    bits |= every
                d[ln] = len(self.sets)
                self.sets.append(bits | tb | (mask << base))
        self.unit_test, self.unit_all, self.unit_count = tb, every, n

    def _ranges_bits(self, text: str) -> int:
        b = 0
        if text == "-":
            return b
        for r in text.split(","):
            lo, _, hi = r.partition("-")
            lo = int(lo)
            hi = int(hi) if hi else lo
            b |= ((1 << (hi - lo + 1)) - 1) << lo
        if b & self.unit_test:
            b |= self.unit_all  # no unit detail
        return b

    def prof_bits(self, path: str, site: str) -> int | None:
        """Tests where the one-operator mutant at `site` ("line:col:op") gives a different
        result at some evaluation in the pinned suite, plus the tests the recording could not
        follow; None when the site is not profiled."""
        if self._prof is None:
            self._prof = {}
            if os.path.exists(self._prof_path):
                with open(self._prof_path) as f:
                    for ln in f:
                        p, _, rest = ln.rstrip("\n").partition("\t")
                        self._prof[p] = rest
        rest = self._prof.get(path)
        if not rest:
            return None
        for item in rest.split("\t"):
            k, _, r = item.partition(" ")
            if k == site:
                return self._ranges_bits(r)
        return None

    def prof_incomplete(self) -> int:
        """Tests that did not run to the end in every recording: profiles say nothing of them."""
        if self._prof is None:
            self.prof_bits("", "")
        return self._ranges_bits(self._prof.get("I", "-").lstrip())

    def units_of(self, bits: int) -> list[int]:
        """Units of the split test that `bits` puts in play."""
        if bits & self.unit_all:
            return list(range(self.unit_count))
        mask = (bits >> self.ntests) & ((1 << self.unit_count) - 1)
        if not mask:
            return list(range(self.unit_count))
        return [i for i in range(self.unit_count) if mask >> i & 1]

    def lines(self, path):
        c = self._lines_cache.get(path)
        if c is None:
            f = self.files.get(path)
            if f is None:
                c = ({}, set())
            else:
                c = ({int(k): v for k, v in f["lines"].items()}, set(f["exec"]))
            self._lines_cache[path] = c
        return c

    def line_bits(self, path, line) -> int:
        d, _ = self.lines(path)
        s = d.get(line)
        return self.sets[s] if s is not None else 0

    def read_bits(self, path) -> int:
        s = self.reads.get(path)
        return self.sets[s] if s is not None else 0

    def from_numbers(self, nums) -> int:
        b = 0
        for n in nums:
            i = self.bit_of_num.get(n)
            if i is None:
                return -1  # a test the coverage map never saw: caller goes broad
            b |= 1 << i
        if b & self.unit_test:
            b |= self.unit_all  # reached by number: no unit detail
        return b

    def to_numbers(self, bits: int) -> set[int]:
        out = set()
        bits &= self.all_bits
        i = 0
        while bits:
            if bits & 1:
                n = self.remap[i]
                if n is not None:
                    out.add(n)
            bits >>= 1
            i += 1
        return out


# --------------------------------------------------------------------------
# python analysis
# --------------------------------------------------------------------------

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.ClassDef) + _FUNC
if hasattr(ast, "TryStar"):
    _COMPOUND = _COMPOUND + (ast.TryStar,)
if hasattr(ast, "Match"):
    _COMPOUND = _COMPOUND + (ast.Match,)


def _dump(node) -> str:
    return ast.dump(node, annotate_fields=False, include_attributes=False)


def _same(a, b) -> bool:
    """_dump(a) == _dump(b), stopping at the first difference."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for f in a._fields:
            if not _same(getattr(a, f, None), getattr(b, f, None)):
                return False
        return True
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if not _same(x, y):
                return False
        return True
    # repr, as the dump has it: 0.0 vs -0.0 and nan differ from ==
    return a == b if isinstance(a, (str, int, bytes)) or a is None else repr(a) == repr(b)


_SWAP = {
    (ast.Is, ast.Eq): "is",
    (ast.Eq, ast.Is): "eq",
    (ast.IsNot, ast.NotEq): "isnot",
    (ast.NotEq, ast.IsNot): "ne",
    (ast.Lt, ast.LtE): "lt",
    (ast.LtE, ast.Lt): "le",
    (ast.Gt, ast.GtE): "gt",
    (ast.GtE, ast.Gt): "ge",
}


class _Flatten(ast.NodeTransformer):
    """`a or (b or c)` is `a or b or c`: same operands, same order, same result."""

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        vals = []
        for v in node.values:
            if isinstance(v, ast.BoolOp) and type(v.op) is type(node.op):
                vals.extend(v.values)
            else:
                vals.append(v)
        node.values = vals
        return node


def _flat_dump(node) -> str:
    import copy

    return _dump(_Flatten().visit(copy.deepcopy(node)))


def _flip(node: ast.BoolOp, k: int):
    """`node` with its k-th operator token (from 1) flipped, as Python parses the result:
    `and` binds tighter than `or`."""
    v = node.values
    if isinstance(node.op, ast.And):
        left = v[:k]
        right = v[k:]
        side = [x[0] if len(x) == 1 else ast.BoolOp(op=ast.And(), values=x) for x in (left, right)]
        return ast.BoolOp(op=ast.Or(), values=side)
    parts = v[: k - 1] + [ast.BoolOp(op=ast.And(), values=[v[k - 1], v[k]])] + v[k + 1 :]
    return parts[0] if len(parts) == 1 else ast.BoolOp(op=ast.Or(), values=parts)


def _bool_flips(a: ast.BoolOp, b) -> list | None:
    """Sites ("line:col:and<k>" / "or<k>") of the and/or tokens in `a` whose flip gives `b`."""
    want = _flat_dump(b)
    out = []
    for c in ast.walk(a):
        if not isinstance(c, ast.BoolOp):
            continue
        op = "and" if isinstance(c.op, ast.And) else "or"
        for k in range(1, len(c.values)):
            m = _flip(c, k)
            if c is a:
                got = _flat_dump(m)
            else:
                swap = _Swap(c, m)
                got = _dump(_Flatten().visit(swap.visit(_deep(a, swap))))
            if got == want:
                # any reading that gives `b` is the same program: one is enough
                return [f"{c.lineno}:{c.col_offset}:{op}{k}"]
    return None


class _Swap(ast.NodeTransformer):
    """Puts `new` where `old` was, in a copy made by _deep."""

    def __init__(self, old, new):
        self.old = old
        self.new = new
        self.mark = None

    def visit(self, node):
        if node is self.mark:
            return self.new
        return super().visit(node)


def _deep(a, swap):
    import copy

    memo = {}
    out = copy.deepcopy(a, memo)
    swap.mark = memo[id(swap.old)]
    return out


def _op_swaps(old, new) -> list | None:
    """Old-tree sites ("line:col:op") where `new` differs from `old` only by is <-> ==,
    is not <-> !=, < <-> <= or > <-> >= in a one-operator comparison, or by flipping and/or
    tokens; None when anything else differs."""
    out = []
    stack = [(old, new)]
    while stack:
        a, b = stack.pop()
        if isinstance(a, ast.BoolOp) and isinstance(b, ast.BoolOp) and (type(a.op) is not type(b.op) or len(a.values) != len(b.values)):
            found = _bool_flips(a, b)
            if found is None:
                return None
            out.extend(found)
            continue
        if type(a) is not type(b):
            return None
        if isinstance(a, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
            op = _SWAP.get((type(a.ops[0]), type(b.ops[0])))
            if op is None:
                return None
            if op in ("is", "eq", "isnot", "ne") and any(isinstance(x, ast.Constant) and type(x.value) not in (type(None), bool, type(...)) for x in (a.left, a.comparators[0])):
                return None  # `is` against a literal compiles with a SyntaxWarning: the swap adds or drops one
            out.append(f"{a.lineno}:{a.col_offset}:{op}")
            stack.append((a.left, b.left))
            stack.append((a.comparators[0], b.comparators[0]))
        elif isinstance(a, ast.AST):
            stack.extend((getattr(a, f, None), getattr(b, f, None)) for f in a._fields)
        elif isinstance(a, list):
            if len(a) != len(b):
                return None
            stack.extend(zip(a, b))
        elif repr(a) != repr(b):  # repr tells 0.0 from -0.0
            return None
    return out


def _code_extent(src: str) -> dict:
    """line -> (first col, end col) in UTF-8 bytes of its code tokens (no comments)."""
    import io
    import tokenize

    out = {}
    skip = (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER)
    lines = src.split("\n")

    def bcol(ln, c):
        return len(lines[ln - 1][:c].encode("utf-8")) if 0 < ln <= len(lines) else c

    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in skip:
                continue
            (sl, sc), (el, ec) = tok.start, tok.end
            for ln in range(sl, el + 1):
                a = bcol(ln, sc) if ln == sl else 0
                b = bcol(ln, ec) if ln == el else 10**9
                cur = out.get(ln)
                out[ln] = (a, b) if cur is None else (min(cur[0], a), max(cur[1], b))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return {}
    return out


def _cover_node(st, lines, extent):
    """Smallest node under statement `st` whose source range holds every code
    token on `lines`, with its field path from `st`; (st, []) if none smaller."""
    rs = [extent[l] for l in lines if l in extent]
    if not rs:
        return None
    lo_l, hi_l = min(lines), max(lines)
    start = (lo_l, extent[lo_l][0]) if lo_l in extent else (lo_l, 0)
    end = (hi_l, extent[hi_l][1]) if hi_l in extent else (hi_l, 10**9)
    best = (st, [])
    stack = [(st, [])]
    while stack:
        node, path = stack.pop()
        for name, val in ast.iter_fields(node):
            items = val if isinstance(val, list) else [val]
            for i, ch in enumerate(items):
                if not isinstance(ch, ast.AST) or isinstance(ch, ast.stmt):
                    continue
                if getattr(ch, "lineno", None) is None or getattr(ch, "end_lineno", None) is None:
                    stack.append((ch, path + [(name, i)]))  # e.g. comprehension, arguments
                    continue
                if (ch.lineno, ch.col_offset) <= start and (ch.end_lineno, ch.end_col_offset) >= end:
                    best = (ch, path + [(name, i)])
                    stack = [(ch, path + [(name, i)])]
                    break
            else:
                continue
            break
    return best


def _dump_hole(node, hole) -> str:
    if node is hole:
        return "<HOLE>"
    if isinstance(node, ast.AST):
        parts = []
        for name, val in ast.iter_fields(node):
            if isinstance(val, list):
                parts.append(name + "=[" + ",".join(_dump_hole(v, hole) for v in val) + "]")
            else:
                parts.append(name + "=" + _dump_hole(val, hole))
        return type(node).__name__ + "(" + ",".join(parts) + ")"
    return repr(node)


def _dump_skel(node) -> str:
    """ast dump with every function body left out."""
    if isinstance(node, ast.AST):
        parts = []
        for name, val in ast.iter_fields(node):
            if name == "body" and isinstance(node, _FUNC + (ast.Lambda,)):
                continue
            if isinstance(val, list):
                parts.append(name + "=[" + ",".join(_dump_skel(v) for v in val) + "]")
            else:
                parts.append(name + "=" + _dump_skel(val))
        return type(node).__name__ + "(" + ",".join(parts) + ")"
    return repr(node)


def _bound_names(tree) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.alias):
            out.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


def _stub(st, parent) -> bool:
    """`st` has only overload decorators, and after it in its body come overload stubs
    of its name, then a def of its name."""
    body = next((b for b in (getattr(parent, f, None) for f in ("body", "orelse", "finalbody")) if isinstance(b, list) and st in b), None)
    if body is None or not all(_dump(d) in _OVERLOAD for d in st.decorator_list):
        return False
    rest = body[body.index(st) + 1:]
    k = 0
    while k < len(rest) and isinstance(rest[k], _FUNC) and rest[k].name == st.name and rest[k].decorator_list and all(_dump(d) in _OVERLOAD for d in rest[k].decorator_list):
        k += 1
    return k < len(rest) and isinstance(rest[k], _FUNC) and rest[k].name == st.name


class PyIndex:
    """Locates, for an old line, its innermost statement and enclosing scope."""

    def __init__(self, tree: ast.Module):
        self.tree = tree
        self.stmts = []  # (lineno, end, node, scope_chain)
        self.lambdas = []
        self._walk(tree.body, [])
        for node in ast.walk(tree):
            if isinstance(node, ast.Lambda):
                self.lambdas.append(node)

    def _walk(self, body, chain):
        for st in body:
            # a def or class starts at its first decorator
            self.stmts.append((min([d.lineno for d in getattr(st, "decorator_list", ())] + [st.lineno]), st.end_lineno, st, chain))
            sub = chain + [st] if isinstance(st, _COMPOUND) else chain
            for name in ("body", "orelse", "finalbody"):
                b = getattr(st, name, None)
                if isinstance(b, list) and b and isinstance(b[0], ast.stmt):
                    self._walk(b, sub)
            if hasattr(st, "handlers"):
                for h in st.handlers:
                    self._walk(h.body, sub + [h])
            if hasattr(ast, "Match") and isinstance(st, ast.Match):
                for c in st.cases:
                    self._walk(c.body, sub + [c])

    def innermost(self, line):
        best = None
        for lo, hi, st, chain in self.stmts:
            if lo <= line <= hi:
                if best is None or (lo >= best[0] and hi <= best[1]):
                    best = (lo, hi, st, chain)
        return best

    def lambda_at(self, line):
        best = None
        for lam in self.lambdas:
            b = lam.body
            if b.lineno <= line <= b.end_lineno:
                if best is None or (b.lineno >= best.body.lineno and b.end_lineno <= best.body.end_lineno):
                    best = lam
        return best


def _header_end(st) -> int:
    """Last line of a compound statement's header."""
    if isinstance(st, _FUNC) or isinstance(st, ast.ClassDef):
        first = st.body[0]
        return first.lineno - 1 if first.lineno > st.lineno else st.lineno
    body = getattr(st, "body", None)
    if isinstance(body, list) and body:
        return max(st.lineno, body[0].lineno - 1)
    return st.lineno


_OVERLOAD = {_dump(ast.parse(x, mode="eval").body) for x in ("t.overload", "typing.overload", "overload")}


def _def_decorators(tree) -> dict:
    """qualname -> each def's decorator dumps, in source order: the last one binds the name."""
    out = {}

    def rec(body, prefix):
        for st in body:
            if isinstance(st, _FUNC):
                out.setdefault(prefix + st.name, []).append([_dump(d) for d in st.decorator_list])
                rec(st.body, prefix + st.name + ".")
            elif isinstance(st, ast.ClassDef):
                rec(st.body, prefix + st.name + ".")
            else:
                for name in ("body", "orelse", "finalbody"):
                    b = getattr(st, name, None)
                    if isinstance(b, list) and b and isinstance(b[0], ast.stmt):
                        rec(b, prefix)
    rec(tree.body, "")
    return out


@dataclass
class Result:
    bits: int = 0
    all: bool = False
    notes: list = field(default_factory=list)
    narrowed: int = 0  # files answered from a mutant profile


class Analyzer:
    def __init__(self, maps: Maps, src_root: str, clsmap=None):
        self.maps = maps
        self.src = src_root
        self.clsmap = clsmap
        self.profiles = True

    def read_old(self, path):
        p = os.path.join(self.src, path)
        try:
            with open(p, encoding="utf-8", newline="") as f:
                return f.read()
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            return None

    # --- entry ---
    def analyze(self, patch_text: str) -> Result:
        res = self._analyze(patch_text)
        if res.narrowed > 1:
            # a profile follows one mutated site: with two, one can change what the other's
            # operands evaluate to
            self.profiles = False
            try:
                res = self._analyze(patch_text)
            finally:
                self.profiles = True
        return res

    def _analyze(self, patch_text: str) -> Result:
        res = Result()
        try:
            fps = parse_patch(patch_text)
        except Exception as e:  # pragma: no cover
            res.all = True
            res.notes.append(f"unparseable patch: {e!r}")
            return res
        if not fps:
            res.notes.append("empty patch")
        for fp in fps:
            try:
                self.analyze_file(fp, res)
            except Exception as e:
                res.all = True
                res.notes.append(f"{fp.path}: error {e!r}")
            if res.all:
                break
        return res

    def analyze_file(self, fp: FilePatch, res: Result):
        path = fp.path
        m = self.maps
        # tests that read the file's bytes during the test
        rb = m.read_bits(path)
        if fp.new_path and fp.new_path != path:
            rb |= m.read_bits(fp.new_path)
        if rb:
            res.bits |= rb
            res.notes.append(f"{path}: read by {bin(rb).count('1')} tests")
        base = os.path.basename(path)
        if fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
            # files appearing/disappearing change directory listings
            for p in (fp.old_path, fp.new_path):
                if p:
                    d = os.path.dirname(p) + "/"
                    s = m.lists.get(d)
                    if s is not None:
                        res.bits |= m.sets[s]
                        res.notes.append(f"{d}: listed by tests")
        if path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
            self.analyze_py(fp, res)
            return
        if path.startswith("tests/fixtures/"):
            # content files: who reads them is in the reads map
            if fp.old_path is None:
                res.notes.append(f"{path}: new fixture, unread")
            return
        if base in ("conftest.py", "pytest.ini", "tox.ini") or (base in ("setup.cfg", "pyproject.toml") and "pytest" in self._patch_text(fp)):
            res.all = True
            res.notes.append(f"{path}: test config")
            return
        if path.endswith(".py") and fp.old_path is None:
            res.notes.append(f"{path}: new python file outside packages")
            return
        res.notes.append(f"{path}: not code ({'read' if rb else 'unread'})")

    def _patch_text(self, fp):
        return "\n".join(t for h in fp.hunks for _, t in h[4])

    def analyze_py(self, fp: FilePatch, res: Result):
        path = fp.path
        old = self.read_old(fp.old_path) if fp.old_path else ""
        if old is None:
            res.notes.append(f"{path}: missing in source tree")
            res.all = True
            return
        if fp.new_path != path and fp.old_path is not None:
            # deleted or moved away: importing the old name fails
            self.unimportable(path, res, "gone")
            return
        blocks: list = []
        new, changed, inserts = apply_hunks(old, fp.hunks, blocks)
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            self.unimportable(path, res, "syntax error")
            return
        if fp.old_path is None:
            # new module: matters to whoever lists its directory (pkgutil
            # walks); importing it by name needs another change
            res.notes.append(f"{path}: new module")
            return
        old_tree = ast.parse(old)
        if _same(old_tree, new_tree):
            res.notes.append(f"{path}: AST unchanged")
            return
        swaps = _op_swaps(old_tree, new_tree) if path.startswith("sqlglot/") else None
        if swaps and all(f"{path}:{x}" in self.maps.eqv for x in swaps):
            # every evaluation in the pinned suite gives the same bool either way, so no
            # test's run changes
            res.notes.append(f"{path}: equivalent comparison swap at {swaps}")
            return
        live = [x for x in swaps or () if f"{path}:{x}" not in self.maps.eqv]
        if len(live) == 1 and self.profiles:
            b = self.maps.prof_bits(path, live[0])
            if b is not None:
                # the tests whose run sees the mutant give another result somewhere; every
                # other test runs exactly as on the pinned tree
                res.narrowed += 1
                site_line = int(live[0].split(":")[0])
                lines_map, _ = self.maps.lines(path)
                cov = 0
                for ln in set(changed) | {site_line}:
                    s = lines_map.get(ln)
                    if s is not None:
                        cov |= self.maps.sets[s]
                res.bits |= b | (self.maps.prof_incomplete() & cov)
                res.notes.append(f"{path}: profiled swap at {live[0]}: {bin(b).count('1')} tests differ")
                return
        triv_old = trivial_lines(old)
        triv_new = trivial_lines(new)
        idx = PyIndex(old_tree)
        self._new_defs = _def_decorators(new_tree)
        self._old_defs = _def_decorators(old_tree)
        m = self.maps
        lines_map, execs = m.lines(path)

        def cov(lo, hi):
            b = 0
            for ln in range(lo, hi + 1):
                s = lines_map.get(ln)
                if s is not None:
                    b |= m.sets[s]
            return b

        def has_exec(lo, hi):
            return any(ln in execs for ln in range(lo, hi + 1))

        new_lines = new.split("\n")
        probes = []
        nidx = None
        ext_o = ext_n = None
        for dels, before, adds in blocks:
            if all(l in triv_old for l in dels) and all(l in triv_new for l in adds):
                continue  # comment/blank-only block
            cd = [l for l in dels if l not in triv_old]
            ca = [l for l in adds if l not in triv_new]
            if cd and ca:
                if nidx is None:
                    nidx = PyIndex(new_tree)
                    ext_o = _code_extent(old)
                    ext_n = _code_extent(new)
                r = self.refine(idx, nidx, cd, ca, ext_o, ext_n, cov, has_exec)
                if r is not None:
                    res.bits |= r
                    continue
            probes.extend((l, None) for l in cd)
            real_adds = [l for l in adds if l not in triv_new]
            if real_adds and all(l in triv_old for l in dels):
                # code added without code removed: an insertion after `before`
                txt = new_lines[real_adds[0] - 1]
                indent = len(txt) - len(txt.lstrip())
                probes.append((self._prev_code(before, triv_old), indent))
        unresolved = []
        for pl, indent in probes:
            if indent is None:
                r = self.locate(idx, pl, path, cov, has_exec, is_insert=False)
            else:
                r = self.locate_ins(idx, pl, indent, cov, has_exec)
            if r is None:
                unresolved.append(pl)
            elif r is ALL:
                res.all = True
                res.notes.append(f"{path}:{pl}: broad")
                return
            else:
                res.bits |= r
        if unresolved:
            new_changed = sorted({n for b in blocks for n in b[2]})
            r = self.class_level(fp, path, old, new, old_tree, new_tree, changed, new_changed, unresolved)
            if r is ALL:
                res.all = True
                res.notes.append(f"{path}: class/module level {unresolved[:5]}")
                return
            res.bits |= r
            res.notes.append(f"{path}: class/module level via clsmap {unresolved[:5]}")

    def module_bound(self, path):
        """Tests a change anywhere in sqlglot module `path` can reach, or ALL.
        Code outside the module sees the change only by importing the module or by
        using what an importer built from it: the tests that import it when run alone,
        plus those that ran code of it or of a module importing it, or used its classes."""
        s = self.maps.need.get(path)
        if s is None:
            return ALL
        bits = self.maps.sets[s]
        if self.clsmap is not None:
            try:
                b = self.maps.from_numbers(self.clsmap.module_users(path))
            except Exception as e:
                log(f"clsmap error {path}: {e!r}")
                return ALL
            if b < 0:
                return ALL
            bits |= b
        return bits

    def unimportable(self, path, res: Result, why):
        """A module that no longer imports fails the tests that import it."""
        if path.startswith("tests/"):
            s = self.maps.test_closure.get(path)
            r = ALL if s is None or path.endswith("__init__.py") else self.maps.sets[s]
        else:
            r = self.module_bound(path)
        if r is ALL:
            res.all = True
            res.notes.append(f"{path}: {why}")
        else:
            res.bits |= r
            res.notes.append(f"{path}: {why}, {bin(r).count('1')} tests need it")

    @staticmethod
    def _prev_code(line, triv):
        while line > 0 and line in triv:
            line -= 1
        return line

    def locate_ins(self, idx: PyIndex, prev, indent, cov, has_exec):
        """Bits for code inserted after old line `prev` at `indent` columns, or
        None when the insertion lands at class/module level."""
        if prev <= 0:
            return None
        cands = [(st.lineno, hi, st, chain) for _, hi, st, chain in idx.stmts if st.lineno <= prev <= hi and st.col_offset <= indent]
        if not cands:
            return None
        lo, hi, st, chain = max(cands, key=lambda c: (c[2].col_offset, c[0]))
        func = None
        for c in reversed(chain):
            if isinstance(c, _FUNC):
                func = c
                break
        if st.col_offset < indent:
            # st opens the block the code is inserted into
            if isinstance(st, _FUNC):
                return cov(st.body[0].lineno, st.end_lineno) if func is None else cov(st.body[0].lineno, st.end_lineno) | cov(func.body[0].lineno, func.end_lineno)
            if isinstance(st, ast.ClassDef):
                return None
            if func is None:
                return None
            # inserted into an if/for/try/... body: reaching it means running the header
            bits = cov(st.lineno, _header_end(st))
            # inside a later clause (else/except/finally) the clause's first statement
            for name in ("orelse", "finalbody"):
                b = getattr(st, name, None)
                if b and b[0].lineno <= prev + 1:
                    bits |= cov(b[0].lineno, b[0].end_lineno)
            for h in getattr(st, "handlers", []) or []:
                bits |= cov(h.lineno, h.end_lineno)
            return bits
        # st is the sibling statement right before the insertion
        if func is None:
            return None
        span = (st.lineno, _header_end(st)) if isinstance(st, _COMPOUND) else (st.lineno, st.end_lineno)
        if not has_exec(*span):
            # a statement without bytecode (docstring, pass, global): whoever runs the block
            parent = chain[-1] if chain else func
            if isinstance(parent, _FUNC):
                return cov(parent.body[0].lineno, parent.end_lineno)
            return cov(parent.lineno, parent.end_lineno)
        return cov(*span)

    def locate(self, idx: PyIndex, line, path, cov, has_exec, is_insert):
        """Bits for a change at old `line` inside a function/lambda body, or
        None if it is at class/module level (or not inside any statement)."""
        lam = idx.lambda_at(line)
        inner = idx.innermost(line)
        if inner is None:
            # blank line / comment between statements: find the scope by neighbors
            return None
        lo, hi, st, chain = inner
        # innermost enclosing function def in chain (+ st itself if def and line in body)
        func = None
        for c in reversed(chain):
            if isinstance(c, _FUNC):
                func = c
                break
        if isinstance(st, _FUNC):
            hdr = _header_end(st)
            dec_lo = min([d.lineno for d in st.decorator_list] + [st.lineno])
            if dec_lo <= line <= hdr:
                # def header: signature, defaults, decorators -> every caller
                bits = cov(st.body[0].lineno, st.end_lineno)
                if func is None:
                    # a function/method defined at class or module level. Same
                    # name and decorators in the new source: only its callers
                    # (those who ran its body) see the change. Otherwise name
                    # dispatch changes: class/module level decides.
                    q = ".".join([c.name for c in chain if isinstance(c, (ast.ClassDef,) + _FUNC)] + [st.name])
                    defs = self._new_defs.get(q)
                    if defs and defs[-1] == [_dump(x) for x in st.decorator_list]:
                        return bits
                    # an overload stub's decorator: the name's last def, unchanged, rebinds it, so the
                    # stubs before it only fill typing's overload registry, which nothing reads
                    if line < st.lineno and defs and _stub(st, chain[-1] if chain else idx.tree) and defs[-1] == self._old_defs[q][-1]:
                        if all(d in _OVERLOAD for ds in defs[:-1] for d in ds):
                            return bits
                    return None
                return bits | cov(func.body[0].lineno, func.end_lineno)
        if lam is not None and (func is None or lam.lineno >= func.lineno):
            b = lam.body
            bits = cov(b.lineno, b.end_lineno)
            return bits
        if func is None:
            return None
        # inside a function body
        if isinstance(st, _COMPOUND) and line <= _header_end(st):
            span = (st.lineno, _header_end(st))
        elif isinstance(st, _COMPOUND):
            # a line inside a compound statement but not in a child statement:
            # else:/finally:/except header lines; use header + following stmt
            span = (st.lineno, _header_end(st))
            nxt = idx.innermost(line + 1)
            bits = cov(*span)
            if nxt is not None:
                bits |= cov(nxt[0], nxt[1] if not isinstance(nxt[2], _COMPOUND) else _header_end(nxt[2]))
            if isinstance(st, ast.Try):
                for h in st.handlers:
                    if h.lineno <= line <= h.end_lineno:
                        bits |= cov(h.lineno, h.end_lineno)
            return bits
        else:
            span = (lo, hi)
        bits = cov(*span)
        if not has_exec(*span):
            # nothing executable recorded on the span: widen to the enclosing block
            parent = chain[-1] if chain else func
            bits |= cov(parent.lineno, parent.end_lineno)
        return bits

    def refine(self, idx, nidx, dels, adds, ext_o, ext_n, cov, has_exec):
        """A replaced run of lines inside one function-body statement that
        differs from the new statement only inside one expression: the tests
        that ran that expression's lines. None when the narrow form doesn't hold."""
        oi = idx.innermost(dels[0])
        ni = nidx.innermost(adds[0])
        if oi is None or ni is None:
            return None
        if any(idx.innermost(l)[2] is not oi[2] for l in dels) or any(nidx.innermost(l)[2] is not ni[2] for l in adds):
            return None
        so, sn = oi[2], ni[2]
        if type(so) is not type(sn) or isinstance(so, _FUNC + (ast.ClassDef,)):
            return None
        if isinstance(so, _COMPOUND) and (max(dels) > _header_end(so) or max(adds) > _header_end(sn)):
            return None
        lam = idx.lambda_at(dels[0])
        in_func = any(isinstance(c, _FUNC) for c in oi[3])
        if not in_func and lam is None:
            return None
        co = _cover_node(so, dels, ext_o)
        cn = _cover_node(sn, adds, ext_n)
        if co is None or cn is None or co[0] is so or co[1] != cn[1]:
            return None
        if _dump_hole(so, co[0]) != _dump_hole(sn, cn[0]):
            return None
        # walk up while the node's lines carry no bytecode of their own
        node, path = co
        while not has_exec(node.lineno, node.end_lineno):
            if not path:
                return None
            path = path[:-1]
            node = so
            for name, i in path:
                v = getattr(node, name)
                node = v[i] if isinstance(v, list) else v
            if node is so:
                return None
        return cov(node.lineno, node.end_lineno)

    _words = None

    def _name_used_elsewhere(self, name, path) -> bool:
        if self._words is None:
            import glob

            words = {}
            pat = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
            for sub in ("sqlglot", "tests"):
                for p in glob.glob(os.path.join(self.src, sub, "**", "*.py"), recursive=True):
                    try:
                        txt = open(p, encoding="utf-8", errors="replace").read()
                    except OSError:
                        continue
                    rel = os.path.relpath(p, self.src)
                    for w in set(pat.findall(txt)):
                        words.setdefault(w, []).append(rel)
            self._words = words
        return bool(self._words.get(name))

    def fresh_defs_only(self, path, old_tree, new_tree) -> bool:
        """The module's structure only gains plain module-level functions under
        names nothing in sqlglot or its tests mentions: nothing can call them."""
        if "/expressions" in path or path.endswith("__init__.py"):
            return False  # modules scanned by name (dir(), registries)
        olds = [_dump_skel(st) for st in old_tree.body]
        news = [_dump_skel(st) for st in new_tree.body]
        if olds == news:
            return False
        bound = _bound_names(old_tree)
        future = any(isinstance(st, ast.ImportFrom) and st.module == "__future__" and any(a.name == "annotations" for a in st.names) for st in old_tree.body)
        i = 0
        extra = []
        for st, d in zip(new_tree.body, news):
            if i < len(olds) and d == olds[i]:
                i += 1
            else:
                extra.append(st)
        if i != len(olds) or not extra:
            return False
        for st in extra:
            if not isinstance(st, _FUNC) or st.decorator_list or getattr(st, "type_params", None):
                return False
            n = st.name
            if n.startswith("__") or n.endswith("_sql") or n in bound or self._name_used_elsewhere(n, path):
                return False
            a = st.args
            for dflt in a.defaults + [x for x in a.kw_defaults if x is not None]:
                if not isinstance(dflt, ast.Constant):
                    return False
            if not future:
                anns = [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs if x.annotation is not None]
                anns += [x.annotation for x in (a.vararg, a.kwarg) if x is not None and x.annotation is not None]
                if st.returns is not None or anns:
                    return False
            if sum(1 for x in extra if isinstance(x, _FUNC) and x.name == n) > 1:
                return False
        return True

    def class_level(self, fp, path, old, new, old_tree, new_tree, changed, new_changed, lines):
        if path.startswith("tests/"):
            # module/class level of a test module: the test files that import it
            s = self.maps.test_closure.get(path)
            return self.maps.sets[s] if s is not None else 0
        if self.fresh_defs_only(path, old_tree, new_tree):
            return 0
        if self.clsmap is not None:
            try:
                r = self.clsmap.affected(path, old, new, sorted(changed), new_changed)
            except Exception as e:
                log(f"clsmap error {path}: {e!r}")
                return ALL
            if r is not None and r != self.clsmap.ALL:
                b = self.maps.from_numbers(r)
                if b >= 0:
                    return b
            if r is None:
                return self.module_bound(path)
        return ALL


# --------------------------------------------------------------------------
# packing
# --------------------------------------------------------------------------

WORKER_MS = 2000


def pack(tests: list[tuple[int, int]], max_workers: int = 8):
    """tests: (number, ms). Minimise max load + 2 s x workers."""
    if not tests:
        return [], 0
    tests = sorted(tests, key=lambda t: -t[1])
    total = sum(ms for _, ms in tests)
    best = None
    for k in range(1, min(max_workers, len(tests)) + 1):
        if best is not None and tests[0][1] + WORKER_MS * k >= best[0]:
            break
        if best is not None and total / k + WORKER_MS * k >= best[0]:
            continue
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for num, ms in tests:
            j = loads.index(min(loads))
            loads[j] += ms
            bins[j].append(num)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1], best[0]


def fill(workers, ms, cands):
    """Put cands (numbers, in order) into the slack under the busiest worker's
    load: free insurance against a missed failure, the cost stays the same."""
    if not workers:
        return 0
    loads = [sum(ms[n] for n in w) for w in workers]
    top = max(loads)
    added = 0
    for n in cands:
        j = loads.index(min(loads))
        if loads[j] + ms[n] > top:
            continue
        workers[j].append(n)
        loads[j] += ms[n]
        added += 1
    return added


def fill_candidates(maps: Maps, patch_text: str, chosen: set) -> list:
    """Unchosen tests that ran any line of a changed file, then those needing its module."""
    try:
        fps = parse_patch(patch_text)
    except Exception:
        return []
    tiers = [0, 0]
    for fp in fps:
        for p in {fp.old_path, fp.new_path} - {None}:
            if not p.endswith(".py"):
                continue
            d, _ = maps.lines(p)
            for s in set(d.values()):
                tiers[0] |= maps.sets[s]
            s = maps.need.get(p)
            if s is not None:
                tiers[1] |= maps.sets[s]
    out, seen = [], set(chosen)
    for bits in tiers:
        nums = sorted(maps.to_numbers(bits) - seen)
        seen.update(nums)
        out.append(nums)
    return out
