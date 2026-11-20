"""Blast Radius selector.

For each patch to sqlglot (pinned eb11aec), pick the tests that could fail and
pack them onto the cheapest number of workers.

How a test set is chosen, per changed file:
  * Python, only comments or blank lines changed (same token stream): no tests.
  * Python that no longer compiles: every test.
  * A changed line inside a function body: the tests that ran that statement
    on the pinned commit (per-test coverage map, see mapgen/).
  * A changed import-time statement (module or class level constant, def
    header, new method): tests that ran code using the names it defines,
    following names derived from them at import time; every test when the
    chain reaches import-time use.
  * A data file (fixtures): the tests that opened it on the pinned commit.
  * Anything unknown: every test. A miss costs 300 s, running everything
    about 37 s, so when in doubt the selector runs everything.
"""

from __future__ import annotations

import ast
import gzip
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
APP = Path(os.environ.get("BLAST_APP", "/app"))
SRC = Path(os.environ.get("BLAST_SRC", str(APP / "src")))
MAX_WORKERS = 8
WORKER_COST_MS = 2000
ALL = None  # "every test"

NO_EFFECT_PREFIXES = ("docs/", "posts/", "benchmarks/", "pdoc/", ".github/")
NO_EFFECT_SUFFIXES = (".md", ".rst", ".txt", ".png", ".jpg", ".svg", ".gif")
NO_EFFECT_FILES = {"LICENSE", "CHANGELOG.md", "README.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", ".gitignore"}


def log(*a):
    print(*a, file=sys.stderr)


class Map:
    def __init__(self, path: Path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
        self.tests = d["tests"]
        self.sets = d["sets"]
        self.lines = d["lines"]
        self.opens = d["opens"]
        self.refs = d["refs"]
        self.derive = d["derive"]
        self.import_use = set(d["import_use"])
        self.by_file: dict[str, set[int]] = {}
        for i, t in enumerate(self.tests):
            self.by_file.setdefault(t.split("::", 1)[0], set()).add(i)

    def set_of(self, sid):
        return ALL if sid == -1 else set(self.sets[sid])

    def line(self, f, ln):
        sid = self.lines.get(f, {}).get(str(ln))
        return set() if sid is None else self.set_of(sid)

    def span(self, f, lo, hi):
        acc: set[int] = set()
        fl = self.lines.get(f, {})
        for ln in range(lo, hi + 1):
            sid = fl.get(str(ln))
            if sid is None:
                continue
            if sid == -1:
                return ALL
            acc.update(self.sets[sid])
        return acc

    def names(self, names, limit=60):
        """Tests that ran code using these names, following import-time derivations."""
        acc: set[int] = set()
        seen = set()
        todo = list(names)
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            if len(seen) > limit or n in self.import_use:
                return ALL
            sid = self.refs.get(n)
            if sid == -1:
                return ALL
            if sid is not None:
                acc.update(self.sets[sid])
            todo.extend(self.derive.get(n, []))
        return acc


def add(acc, s):
    if acc is ALL or s is ALL:
        return ALL
    acc |= s
    return acc


# ---------------------------------------------------------------- patches

def parse_patch(text: str):
    """-> list of (old_path, new_path, hunks); hunk = (old_start, new_start, [(tag, text)])."""
    files = []
    cur = None
    hunk = None
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": []}
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", raw)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            hunk = None
            continue
        if cur is None:
            continue
        if raw.startswith("--- ") and hunk is None:
            p = raw[4:].strip()
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            continue
        if raw.startswith("+++ ") and hunk is None:
            p = raw[4:].strip()
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            hunk = (int(m.group(1)), int(m.group(2)), [])
            cur["hunks"].append(hunk)
            continue
        if hunk is not None and raw[:1] in (" ", "-", "+"):
            hunk[2].append((raw[0], raw[1:]))
        elif hunk is not None and raw.startswith("\\"):
            continue
        elif raw.startswith(("new file", "deleted file", "similarity", "rename ", "index ", "old mode", "new mode", "Binary")):
            cur.setdefault("meta", []).append(raw)
    return files


def apply_hunks(old_text: str, hunks):
    old = old_text.split("\n")
    out = []
    pos = 0  # 0-based index into old
    for old_start, _new_start, lines in hunks:
        start = max(old_start - 1, 0) if any(t != "+" for t, _ in lines) else old_start
        out.extend(old[pos:start])
        pos = start
        for tag, text in lines:
            if tag == " ":
                out.append(old[pos] if pos < len(old) else text)
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(text)
    out.extend(old[pos:])
    return "\n".join(out)


def changed_lines(hunks):
    """-> (deleted old line numbers, [(new line no, old anchor line no)] for added lines)."""
    dels, adds = [], []
    for old_start, new_start, lines in hunks:
        o, n = old_start, new_start
        if all(t == "+" for t, _ in lines):
            o = old_start + 1  # "-k,0" means insertion after old line k
        for tag, text in lines:
            if tag == " ":
                o += 1
                n += 1
            elif tag == "-":
                dels.append(o)
                o += 1
            else:
                adds.append((n, o))
                n += 1
    return dels, adds


# ---------------------------------------------------------------- python analysis

def code_tokens(text):
    skip = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING}
    return [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type not in skip]


def is_blank_or_comment(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


COMPOUND_FIELDS = ("body", "orelse", "finalbody")


def child_blocks(stmt):
    for f in COMPOUND_FIELDS:
        b = getattr(stmt, f, None)
        if b:
            yield b
    for h in getattr(stmt, "handlers", []) or []:
        yield h.body
    for c in getattr(stmt, "cases", []) or []:
        yield c.body


def header_span(stmt):
    start = min([stmt.lineno] + [d.lineno for d in getattr(stmt, "decorator_list", [])])
    blocks = list(child_blocks(stmt))
    if not blocks:
        return start, stmt.end_lineno
    first = min(b[0].lineno for b in blocks if b)
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        body0 = stmt.body[0]
        first = min(getattr(body0, "decorator_list", []) and [d.lineno for d in body0.decorator_list] or [body0.lineno])
    return start, max(start, first - 1)


def locate(tree, ln):
    """-> (stmt, in_function, is_header, enclosing top-level def) for the innermost statement at line ln."""
    def walk(body, in_func, owner):
        for stmt in body:
            lo = min([stmt.lineno] + [d.lineno for d in getattr(stmt, "decorator_list", [])])
            if not (lo <= ln <= stmt.end_lineno):
                continue
            hlo, hhi = header_span(stmt)
            is_def = isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
            if hlo <= ln <= hhi:
                return stmt, in_func, True, owner
            for block in child_blocks(stmt):
                r = walk(block, in_func or is_def, owner if (in_func or not is_def) else stmt)
                if r:
                    return r
            return stmt, in_func, True, owner  # e.g. an `else:` line
        return None
    return walk(tree.body, False, None)


def func_body_lines(node):
    """Line ranges of every function body inside node (or node itself if a function)."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            out.append((n.body[0].lineno, n.end_lineno))
    return out


def defined_names(stmt):
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [stmt.name]
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    names = []
    for tg in targets:
        for n in ast.walk(tg):
            if isinstance(n, ast.Name):
                names.append(n.id)
            elif isinstance(n, ast.Attribute):
                names.append(n.attr)
    return names


class PyFile:
    def __init__(self, m: Map, path: str, old_text: str, new_text: str):
        self.m, self.path = m, path
        self.old_text, self.new_text = old_text, new_text
        self.old_tree = ast.parse(old_text)
        self.new_tree = ast.parse(new_text)
        self.old_lines = old_text.split("\n")
        self.new_lines = new_text.split("\n")
        self.own_tests = m.by_file.get(path, set())

    def import_level(self, stmt, tree_is_old):
        """Tests affected by a changed import-time statement."""
        m = self.m
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            return ALL
        if self.own_tests and self.path.startswith("tests/"):
            acc = set(self.own_tests)
            names = defined_names(stmt)
            return add(acc, m.names(names)) if names else acc
        names = defined_names(stmt)
        if not names:
            return ALL
        acc = m.names(names)
        if acc is ALL:
            return ALL
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if tree_is_old:
                for lo, hi in func_body_lines(stmt):
                    acc = add(acc, m.span(self.path, lo, hi))
                    if acc is ALL:
                        return ALL
            elif not acc:
                return ALL  # a new definition nothing references by name: dynamic dispatch?
        return acc

    def old_line(self, ln):
        if ln < 1 or ln > len(self.old_lines) or is_blank_or_comment(self.old_lines[ln - 1]):
            return set()
        r = locate(self.old_tree, ln)
        if r is None:
            return set()
        stmt, in_func, is_header, _owner = r
        if not in_func:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not is_header:
                return set()
            return self.import_level(stmt, True)
        lo, hi = header_span(stmt) if is_header else (stmt.lineno, stmt.end_lineno)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_header:
            # nested def header: runs whenever the enclosing body reaches it
            return self.m.span(self.path, lo, hi)
        return self.m.span(self.path, lo, hi)

    def added_line(self, new_ln, old_anchor):
        if is_blank_or_comment(self.new_lines[new_ln - 1]):
            return set()
        r = locate(self.new_tree, new_ln)
        if r is None:
            return set()
        stmt, in_func, is_header, _owner = r
        if not in_func:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not is_header:
                return set()
            return self.import_level(stmt, False)
        # Inside a function: runs when its neighbours on the pinned commit ran.
        acc: set[int] = set()
        for a in (old_anchor - 1, old_anchor):
            if 1 <= a <= len(self.old_lines) and not is_blank_or_comment(self.old_lines[a - 1]):
                ra = locate(self.old_tree, a)
                if ra is None:
                    continue
                s2, f2, h2, _ = ra
                if isinstance(s2, (ast.FunctionDef, ast.AsyncFunctionDef)) and h2 and not f2:
                    for lo, hi in func_body_lines(s2)[:1]:
                        acc = add(acc, self.m.span(self.path, lo, hi))
                else:
                    acc = add(acc, self.old_line(a))
                if acc is ALL:
                    return ALL
        if not acc:
            # neighbours never ran; widen to the enclosing statement block in the old tree
            ra = locate(self.old_tree, max(1, min(old_anchor, len(self.old_lines))))
            if ra is not None and ra[1]:
                lo, hi = ra[0].lineno, ra[0].end_lineno
                acc = self.m.span(self.path, lo, hi)
        return acc


def select_file(m: Map, f) -> set | None:
    old_path, new_path = f["old"], f["new"]
    path = new_path or old_path
    if path is None:
        return ALL
    if old_path and new_path and old_path != new_path:
        return ALL  # rename
    if path in NO_EFFECT_FILES or path.startswith(NO_EFFECT_PREFIXES) or path.endswith(NO_EFFECT_SUFFIXES):
        if path not in m.opens:
            return set()
    if not path.endswith(".py"):
        if path in m.opens:
            return m.set_of(m.opens[path])
        if path.startswith("tests/") and old_path is not None:
            return set()  # a data file under tests/ that no test reads
        return ALL
    # Python
    if path.startswith(("sqlglot/", "tests/")) is False:
        if path in m.opens:
            return m.set_of(m.opens[path])
        return ALL
    if old_path is None or new_path is None:
        return ALL  # new or deleted module
    src = SRC / path
    if not src.is_file():
        return ALL
    old_text = src.read_text(encoding="utf-8")
    new_text = apply_hunks(old_text, f["hunks"])
    try:
        compile(new_text, path, "exec")
    except SyntaxError:
        return ALL if not path.startswith("tests/") or not m.by_file.get(path) else set(m.by_file[path])
    try:
        if code_tokens(old_text) == code_tokens(new_text):
            return set()
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return ALL
    pf = PyFile(m, path, old_text, new_text)
    dels, adds = changed_lines(f["hunks"])
    acc: set[int] = set()
    for ln in dels:
        acc = add(acc, pf.old_line(ln))
        if acc is ALL:
            return ALL
    for n, o in adds:
        acc = add(acc, pf.added_line(n, o))
        if acc is ALL:
            return ALL
    return acc


def select(m: Map, patch_text: str):
    files = parse_patch(patch_text)
    if not files:
        return ALL
    acc: set[int] = set()
    for f in files:
        try:
            s = select_file(m, f)
        except Exception as e:  # noqa: BLE001 - any surprise means "run everything"
            log(f"error on {f.get('new') or f.get('old')}: {e!r}")
            s = ALL
        acc = add(acc, s)
        if acc is ALL:
            return ALL
    return acc


# ---------------------------------------------------------------- packing

def pack(tests, count):
    workers = [[] for _ in range(count)]
    loads = [0] * count
    for number, ms in sorted(tests, key=lambda t: -t[1]):
        i = loads.index(min(loads))
        workers[i].append(number)
        loads[i] += ms
    workers = [w for w in workers if w]
    return max(loads) + WORKER_COST_MS * len(workers), workers


def best_split(tests):
    if not tests:
        return []
    return min((pack(tests, k) for k in range(1, MAX_WORKERS + 1)), key=lambda r: r[0])[1]


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    number = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    every = [(t["number"], ms[t["number"]]) for t in tests]
    try:
        m = Map(APP / "map.json.gz")
    except Exception as e:  # noqa: BLE001
        log(f"map unavailable ({e!r}); running everything")
        m = None
    all_split = best_split(every)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        chosen = ALL
        if m is not None:
            try:
                chosen = select(m, patch.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:  # noqa: BLE001
                log(f"{patch.stem}: {e!r}")
                chosen = ALL
        if chosen is ALL:
            workers = all_split
            log(f"{patch.stem}: all tests")
        else:
            nums = sorted({number[m.tests[i]] for i in chosen if m.tests[i] in number})
            workers = best_split([(n, ms[n]) for n in nums])
            log(f"{patch.stem}: {len(nums)} tests")
        print(json.dumps({"commit": patch.stem, "workers": workers}))


if __name__ == "__main__":
    main()
