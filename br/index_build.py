"""Build-time index: turns the coverage map + sqlglot source into fast lookup tables.

Run during `docker build` (not charged). Output: a pickle with, per source file,
for every line the set of tests (as an int bitset over map test order) that a
change on that line can affect, plus helper tables for the runtime.
"""

import ast
import gzip
import json
import os
import pickle
import re
import sys
from collections import defaultdict

SRC = sys.argv[1]  # sqlglot checkout root
MAP = sys.argv[2]  # map.json.gz
OUT = sys.argv[3]  # index.pkl

with gzip.open(MAP, "rt") as fh:
    m = json.load(fh)

TESTS = m["tests"]
NT = len(TESTS)
ALL = (1 << NT) - 1
cov = {f: {int(l): int(b, 16) for l, b in d.items()} for f, d in m["lines"].items()}
imp = {f: set(v) for f, v in m["import_lines"].items()}
reads = {p: int(b, 16) for p, b in m["reads"].items()}
dial_users = {p: int(b, 16) for p, b in m["dialects"].items()}  # "module.Class" -> tests

# ---------------------------------------------------------------- source scan
py_files = []
for base in ("sqlglot", "tests"):
    for root, dirs, files in os.walk(os.path.join(SRC, base)):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(root, f), SRC).replace("\\", "/"))

sources = {}
trees = {}
for f in py_files:
    with open(os.path.join(SRC, f), "rb") as fh:
        b = fh.read()
    sources[f] = b.decode("utf-8", "replace").splitlines()
    try:
        trees[f] = ast.parse(b)
    except SyntaxError:
        pass


def union_lines(f, lo, hi):
    d = cov.get(f, {})
    b = 0
    for l in range(lo, hi + 1):
        b |= d.get(l, 0)
    return b


# identifier -> runtime-covered lines referencing it (any sqlglot file)
ident_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
refs = defaultdict(int)
for f, d in cov.items():
    src = sources.get(f)
    if not src:
        continue
    for l, b in d.items():
        if 1 <= l <= len(src):
            for w in set(ident_re.findall(src[l - 1])):
                refs[w] |= b

# class name -> (file, node); simple-name class hierarchy inside sqlglot
classes = defaultdict(list)
for f, tree in trees.items():
    if not f.startswith("sqlglot/"):
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes[node.name].append((f, node))


def base_names(node):
    out = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            out.append(b.id)
        elif isinstance(b, ast.Attribute):
            out.append(b.attr)
    return out


subclasses = defaultdict(set)
for name, lst in classes.items():
    for f, node in lst:
        for b in base_names(node):
            subclasses[b].add(name)


def all_subclasses(name):
    seen = {name}
    stack = [name]
    while stack:
        c = stack.pop()
        for s in subclasses.get(c, ()):
            if s not in seen:
                seen.add(s)
                stack.append(s)
    return seen


def class_method_cov(name):
    b = 0
    for f, node in classes.get(name, []):
        for item in ast.walk(node):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                b |= union_lines(f, item.lineno, item.end_lineno)
    return b


def class_method_cov_node(node):
    b = 0
    for item in ast.walk(node):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            b |= union_lines(CUR_FILE[0], item.lineno, item.end_lineno)
    return b


CUR_FILE = [None]


# dialect family: tests that instantiated a dialect whose module set includes file f
def module_of(f):
    return f[:-3].replace("/", ".").removesuffix(".__init__")


def class_users(name, f):
    """Tests that plausibly use class `name` (or its subclasses) at runtime."""
    b = 0
    subs = all_subclasses(name)
    for s in subs:
        b |= class_method_cov(s)
        b |= refs.get(s, 0)
    # dialect instantiation records: Dialect subclasses
    for key, users in dial_users.items():
        cls = key.rsplit(".", 1)[1]
        if cls in subs:
            b |= users
    # parser/generator/tokenizer/typing modules for a dialect: map by file stem
    stem = os.path.basename(f)[:-3]
    if f.startswith(("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/tokenizers/", "sqlglot/typing/", "sqlglot/dialects/")):
        for key, users in dial_users.items():
            mod = key.rsplit(".", 1)[0]
            if mod.endswith("." + stem):
                b |= users
    return b


def module_users(f):
    b = 0
    for l, bb in cov.get(f, {}).items():
        b |= bb
    return b


# ---------------------------------------------------------------- per-line table
def targets_of(stmt):
    names = []
    tg = []
    if isinstance(stmt, ast.Assign):
        tg = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        tg = [stmt.target]
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in stmt.names]
    for t in tg:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.append(n.id)
            elif isinstance(n, ast.Attribute):
                names.append(n.attr)
    return names


# identifiers used in each test's own source (tests can use module constants directly)
test_refs = defaultdict(int)
_tpos = {t: i for i, t in enumerate(TESTS)}
for f, tree in trees.items():
    if not f.startswith("tests/"):
        continue
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    i = _tpos.get(f"{f}::{node.name}::{item.name}")
                    if i is None:
                        continue
                    for l in range(item.lineno, item.end_lineno + 1):
                        for w in set(ident_re.findall(sources[f][l - 1])):
                            test_refs[w] |= 1 << i
IMPORT_TIME_EXTRA = {}  # filled below once eager modules are known

line_info = {}  # file -> list indexed by line: bitset of affected tests


def build_file(f, tree):
    CUR_FILE[0] = f
    n = len(sources[f])
    res = [0] * (n + 2)
    exp = [None] * (n + 2)  # runtime statements: (lo, hi) line range a change there affects
    is_test = f.startswith("tests/")

    def mark(lo, hi, b):
        for l in range(max(lo, 1), min(hi, n) + 1):
            res[l] |= b

    def visit_body(body, ctx_class, ctx_func, ctx_func_cov, ctx_users=None):
        for st in body:
            lo = st.lineno
            if getattr(st, "decorator_list", None):
                lo = min([lo] + [d.lineno for d in st.decorator_list])
            hi = st.end_lineno
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fcov = union_lines(f, st.lineno, st.end_lineno)
                body_lo = st.body[0].lineno
                if is_test:
                    fcov = 0
                # header (decorators, def line, args, defaults)
                hdr = fcov
                if ctx_class is not None or ctx_func is None:
                    # definition executes at import / class creation
                    pass
                mark(lo, body_lo - 1, hdr)
                visit_body(st.body, None, st, fcov)
                continue
            if isinstance(st, ast.ClassDef):
                body_lo = st.body[0].lineno
                if is_test:
                    users = 0
                elif ctx_users is not None:
                    # nested class (e.g. Dialect.Tokenizer): users of the outer class
                    users = ctx_users | class_method_cov_node(st)
                else:
                    users = class_users(st.name, f)
                mark(lo, body_lo - 1, users)
                visit_body(st.body, st, None, 0, users)
                continue
            # header range for compound statements
            sub_bodies = []
            for fld in ("body", "orelse", "finalbody", "handlers", "cases"):
                v = getattr(st, fld, None)
                if v:
                    sub_bodies.append(v)
            if sub_bodies:
                first_child = min(s[0].lineno for s in sub_bodies)
                hdr_hi = first_child - 1
            else:
                hdr_hi = hi
            if ctx_func is not None:
                # runtime statement: tests executing it (whole header range)
                b = union_lines(f, lo, max(lo, hdr_hi))
                at_import = any(l in imp.get(f, ()) for l in range(lo, max(lo, hdr_hi) + 1))
                if at_import:
                    b |= module_users(f) | IMPORT_TIME_EXTRA.get(f, 0)
                mark(lo, max(lo, hdr_hi), b)
                for l in range(lo, max(lo, hdr_hi) + 1):
                    if l <= n:
                        exp[l] = None if at_import else (lo, max(lo, hdr_hi))
                # else/elif/except/finally keyword lines: tests executing the statement
                if sub_bodies:
                    whole = union_lines(f, lo, hi)
                    for l in range(hdr_hi + 1, hi + 1):
                        res[l] |= 0  # filled by children; keyword lines below
                    stmt_b = b
                    for fld in ("orelse", "finalbody", "handlers"):
                        v = getattr(st, fld, None)
                        if v:
                            kw = v[0].lineno - 1
                            res[kw] |= stmt_b | whole
                            exp[kw] = (lo, hi)
                for v in sub_bodies:
                    if v and isinstance(v[0], ast.AST) and hasattr(v[0], "lineno"):
                        visit_body(
                            [x for x in v if isinstance(x, ast.stmt)] or [],
                            None,
                            ctx_func,
                            ctx_func_cov,
                        )
                        for h in v:
                            if isinstance(h, ast.ExceptHandler):
                                res[h.lineno] |= union_lines(f, h.lineno, h.end_lineno) | b
                                exp[h.lineno] = (lo, hi)
                                visit_body(h.body, None, ctx_func, ctx_func_cov)
                continue
            # import-time statement (module level or class body)
            names = targets_of(st)
            if ctx_class is not None:
                users = ctx_users if ctx_users is not None else class_users(ctx_class.name, f)
                rb = 0
                for nm in names:
                    rb |= refs.get(nm, 0)
                b = (rb & users) if names and rb else users
            else:
                b = 0
                for nm in names:
                    b |= refs.get(nm, 0)
                if not names or not b:
                    b = module_users(f)
            for nm in names:
                b |= test_refs.get(nm, 0)
            # runtime coverage of lines inside (lambdas / comprehensions in class bodies)
            b |= union_lines(f, lo, hi)
            mark(lo, max(lo, hdr_hi), b)
            if sub_bodies:
                for v in sub_bodies:
                    visit_body([x for x in v if isinstance(x, ast.stmt)], ctx_class, None, 0, ctx_users)

    visit_body(tree.body, None, None, 0)
    EXPAND[f] = exp
    return res


import subprocess as _sp

_EAGER_SCRIPT = "import sys; sys.path.insert(0, '.'); import sqlglot\nfor m in list(sys.modules.values()):\n    if getattr(m, '__file__', None) and m.__name__.startswith('sqlglot'):\n        print(m.__file__)\n"
_eager0 = _sp.run([sys.executable, "-c", _EAGER_SCRIPT], cwd=SRC, capture_output=True, text=True).stdout.splitlines()
_ALLBITS = (1 << NT) - 1
for _x in _eager0:
    # functions run at import of an eagerly imported module build state every test uses
    IMPORT_TIME_EXTRA[os.path.relpath(_x, SRC).replace(os.sep, "/")] = _ALLBITS

EXPAND = {}
for f, tree in trees.items():
    if f.startswith("sqlglot/"):
        line_info[f] = build_file(f, tree)

# ---------------------------------------------------------------- fixture-entry units
units = {}
UNITS = sys.argv[4] if len(sys.argv) > 4 else None
if UNITS and os.path.exists(UNITS):
    with gzip.open(UNITS, "rt") as fh:
        um = json.load(fh)
    pos = {t: i for i, t in enumerate(TESTS)}
    for tid, info in um.items():
        if tid not in pos or not info.get("ok") or not all(u[2] for u in info["units"]):
            continue
        keys = []
        inv = defaultdict(lambda: defaultdict(int))
        for j, (fn, k, passed, diff) in enumerate(info["units"]):
            keys.append((fn, k))
            for f, ls in diff.items():
                d = inv[f]
                for l in ls:
                    d[l] |= 1 << j
        # lines the whole-test run executes that neither the empty run nor any single
        # entry run reached (cross-entry state): a change there forces a full run
        bit = 1 << pos[tid]
        gaps = {}
        for f, d in cov.items():
            bs = set(info["base"].get(f, ()))
            iv = inv.get(f, {})
            g = {l for l, b in d.items() if b & bit and l not in bs and l not in iv}
            if g:
                gaps[f] = g
        units[pos[tid]] = {
            "gaps": gaps,
            "keys": keys,
            "base": {f: set(ls) for f, ls in info["base"].items()},
            "inv": {f: dict(d) for f, d in inv.items()},
        }
print("sliceable tests", len(units), file=sys.stderr)

# test files: line -> tests (test methods by AST; module/class level -> all tests in file)
test_ids_by_file = defaultdict(list)
for i, t in enumerate(TESTS):
    test_ids_by_file[t.split("::")[0]].append(i)

test_line = {}
for f, tree in trees.items():
    if not f.startswith("tests/"):
        continue
    n = len(sources[f])
    file_bits = 0
    for i in test_ids_by_file.get(f, []):
        file_bits |= 1 << i
    res = [file_bits] * (n + 2)
    idmap = {TESTS[i].split("::", 1)[1]: i for i in test_ids_by_file.get(f, [])}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            cls_bits = 0
            for k, i in idmap.items():
                if k.split("::")[0] == node.name:
                    cls_bits |= 1 << i
            for l in range(node.lineno, node.end_lineno + 1):
                res[l] = cls_bits or file_bits
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    key = f"{node.name}::{item.name}"
                    lo = min([item.lineno] + [d.lineno for d in item.decorator_list])
                    for l in range(lo, item.lineno):
                        res[l] = file_bits  # decorators run when the class is created
                    lo = item.lineno
                    if key in idmap:
                        b = 1 << idmap[key]
                    elif item.name.startswith("test"):
                        b = 0
                    else:
                        b = cls_bits or file_bits  # helper method used by tests
                    for l in range(lo, item.end_lineno + 1):
                        res[l] = b
    test_line[f] = res

# which test files import which test helper modules (by text)
importers = defaultdict(int)
for f in trees:
    if not f.startswith("tests/"):
        continue
    text = "\n".join(sources[f])
    for g in trees:
        if g.startswith("tests/") and g != f:
            mod = g[:-3].replace("/", ".")
            short = mod.rsplit(".", 1)[1]
            if re.search(r"\b" + re.escape(mod) + r"\b", text) or re.search(
                r"from\s+\.?\s*import\s+.*\b" + re.escape(short) + r"\b|from\s+tests\s+import\s+.*\b" + re.escape(short) + r"\b", text
            ):
                b = 0
                for i in test_ids_by_file.get(f, []):
                    b |= 1 << i
                importers[g] |= b

# importers are transitive: a helper imported by a helper breaks all their importers
_mod_tests = {g: 0 for g in trees if g.startswith("tests/")}
for g in _mod_tests:
    b = 0
    for i in test_ids_by_file.get(g, []):
        b |= 1 << i
    _mod_tests[g] = b
_imports_of = defaultdict(set)  # helper -> test modules importing it directly
for f in trees:
    if not f.startswith("tests/"):
        continue
    text = "\n".join(sources[f])
    for g in trees:
        if g.startswith("tests/") and g != f:
            mod = g[:-3].replace("/", ".")
            short = mod.rsplit(".", 1)[1]
            if re.search(r"\b" + re.escape(mod) + r"\b", text) or re.search(
                r"from\s+\.?\s*import\s+.*\b" + re.escape(short) + r"\b|from\s+tests\s+import\s+.*\b" + re.escape(short) + r"\b", text
            ):
                _imports_of[g].add(f)
for g in list(_imports_of):
    seen = set()
    stack = list(_imports_of[g])
    while stack:
        h = stack.pop()
        if h in seen:
            continue
        seen.add(h)
        stack.extend(_imports_of.get(h, ()))
    b = 0
    for h in seen:
        b |= _mod_tests.get(h, 0)
    importers[g] = importers.get(g, 0) | b


def ids_matching(pred):
    b = 0
    for i, t in enumerate(TESTS):
        if pred(t):
            b |= 1 << i
    return b


import subprocess

_eager = subprocess.run(
    [
        sys.executable,
        "-c",
        "import sys; sys.path.insert(0, '.'); import sqlglot\n"
        "for m in list(sys.modules.values()):\n"
        "    if getattr(m, '__file__', None) and m.__name__.startswith('sqlglot'):\n"
        "        print(m.__file__)\n",
    ],
    cwd=SRC, capture_output=True, text=True,
).stdout.splitlines()
eager_files = sorted({os.path.relpath(x, SRC).replace("\\", "/") for x in _eager})
print("eager modules", len(eager_files), file=sys.stderr)

special = {
    "lazy_load": ids_matching(lambda t: t.endswith("::test_lazy_load")),
    "sqlglotc": ids_matching(lambda t: t.startswith("tests/test_sqlglotc.py")),
}
_ENUM_SCRIPT = chr(10).join([
    "import sys, enum, inspect, pkgutil, importlib",
    "sys.path.insert(0, '.')",
    "import sqlglot",
    "names = set()",
    "for m in pkgutil.walk_packages(sqlglot.__path__, 'sqlglot.'):",
    "    try:",
    "        mod = importlib.import_module(m.name)",
    "    except Exception:",
    "        continue",
    "    for v in vars(mod).values():",
    "        if inspect.isclass(v) and issubclass(v, enum.Enum) and v.__module__.startswith('sqlglot'):",
    "            names.add(v.__name__)",
    "print(' '.join(sorted(names)))",
])
enum_names = set(_sp.run([sys.executable, "-c", _ENUM_SCRIPT], cwd=SRC, capture_output=True, text=True).stdout.split())
print("enum classes", len(enum_names), file=sys.stderr)
idx = {
    "enum_names": enum_names,
    "expand": EXPAND,
    "units": units,
    "eager_files": set(eager_files),
    "special": special,
    "import_lines": imp,
    "tests": TESTS,
    "line_info": line_info,
    "test_line": test_line,
    "reads": reads,
    "importers": dict(importers),
    "module_users": {f: module_users(f) for f in line_info},
    "test_ids_by_file": dict(test_ids_by_file),
}
# intern identical bitsets so pickle stores each distinct value once
_canon = {}


def _intern(o):
    if isinstance(o, int):
        return _canon.setdefault(o, o)
    if isinstance(o, list):
        return [_intern(x) for x in o]
    if isinstance(o, dict):
        return {k: _intern(v) for k, v in o.items()}
    return o


for k in ("line_info", "test_line", "reads", "importers", "module_users", "special"):
    idx[k] = _intern(idx[k])

with open(OUT, "wb") as fh:
    pickle.dump(idx, fh, protocol=pickle.HIGHEST_PROTOCOL)
print("index built", len(line_info), "files", file=sys.stderr)
