"""Turn the raw per-phase coverage (mapper.py output) into the selector's map.

Usage: python make_map.py <raw_dir> <sqlglot_src> <out map.json.gz>

Map contents (test sets are indexes into "sets"; set -1 = every test):
  tests:  pytest node ids in run order
  sets:   list of sorted test-index lists
  lines:  {file: {line: set}}  tests that ran the line (call phase = that test,
          setup/teardown = its whole class, collection = its whole test file,
          outside any test = every test)
  opens:  {path: set}          tests that opened a non-Python file of the repo
  refs:   {name: set}          tests that ran a function-body line using the name
  derive: {name: [names]}      names defined at import time by statements using the name
  import_use: [names]          names used by import-time statements that define nothing nameable
"""
import ast, gzip, json, os, re, sys
from collections import defaultdict

raw, src, out = sys.argv[1], sys.argv[2], sys.argv[3]
cov = json.load(open(os.path.join(raw, "cov.json")))
opens_raw = json.load(open(os.path.join(raw, "opens.json")))
tests = json.load(open(os.path.join(raw, "order.json")))
tindex = {t: i for i, t in enumerate(tests)}
by_class, by_file = defaultdict(list), defaultdict(list)
for t, i in tindex.items():
    by_class[t.rsplit("::", 1)[0]].append(i)
    by_file[t.split("::", 1)[0]].append(i)
ALL = None


def translate(ctx, own_file=None):
    """Context name -> set of test indexes, or None for every test.

    Code that ran while a test file was being collected ran at import time:
    for that test file's own lines it concerns that file's tests, for sqlglot
    code it can break (or change) every module that imports it, so every test."""
    if ctx == "<import>":
        return ALL
    kind, rest = ctx.split("|", 1)
    if kind == "C":
        return set(by_file.get(rest, [])) if rest == own_file else ALL
    if rest not in tindex:
        return set()
    if kind == "R":
        return {tindex[rest]}
    return set(by_class[rest.rsplit("::", 1)[0]])


ctx_sets = [translate(c) for c in cov["contexts"]]
ctx_names = cov["contexts"]
sets, set_ids = [], {}


def sid(s):
    if s is ALL:
        return -1
    key = tuple(sorted(s))
    if key not in set_ids:
        set_ids[key] = len(sets)
        sets.append(list(key))
    return set_ids[key]


def union(ctx_idx, own_file=None):
    acc = set()
    for c in ctx_idx:
        s = ctx_sets[c] if own_file is None else translate(ctx_names[c], own_file)
        if s is ALL:
            return ALL
        acc |= s
    return acc


lines = {}
line_sets = {}
for f, lm in cov["files"].items():
    own = f if f.startswith("tests/") else None
    line_sets[f] = {int(l): union(cs, own) for l, cs in lm.items()}
    lines[f] = {l: sid(s) for l, s in line_sets[f].items()}

opens = defaultdict(set)
opens_all = set()
for ctx, paths in opens_raw.items():
    s = translate(ctx)
    for p in paths:
        p = p.replace(os.sep, "/")
        if p.endswith(".py"):
            continue
        if s is ALL:
            opens_all.add(p)
        else:
            opens[p] |= s
opens_out = {p: sid(s) for p, s in opens.items()}
for p in opens_all:
    opens_out[p] = -1

# Name references, from the pinned source's syntax trees.
refs = defaultdict(set)
refs_all = set()
derive = defaultdict(set)
import_use = set()
qrefs = defaultdict(set)    # "Cls.attr" -> tests that ran a function-body line reading it
qrefs_all = set()
qderive = defaultdict(set)  # "Cls.attr" -> names defined by import-time statements reading it


def qual_keys(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            v = n.value
            base = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else None
            if base:
                yield n, base + "." + n.attr


def names_in(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.Attribute):
            yield n.attr


def defined_names(stmt):
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [stmt.name]
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    out = []
    for tg in targets:
        for n in ast.walk(tg):
            if isinstance(n, ast.Name):
                out.append(n.id)
            elif isinstance(n, ast.Attribute):
                out.append(n.attr)
    return out


def visit_level(body, fname):
    """Import-time statements: module and class bodies."""
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            header = stmt.bases + stmt.keywords + stmt.decorator_list
            used = {x for h in header for x in names_in(h)}
            for u in used:
                derive[u].add(stmt.name)
            visit_level(stmt.body, fname)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            header = stmt.decorator_list + stmt.args.defaults + [d for d in stmt.args.kw_defaults if d]
            for h in header:
                for u in names_in(h):
                    derive[u].add(stmt.name)
            visit_body(stmt.body, fname)
        elif isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            # import-time control flow: treat contents as import-time too
            for u in names_in(stmt):
                import_use.add(u)
            for field in ("body", "orelse", "finalbody"):
                visit_level(getattr(stmt, field, []) or [], fname)
            for h in getattr(stmt, "handlers", []) or []:
                visit_level(h.body, fname)
        else:
            defs = defined_names(stmt)
            for _n, k in qual_keys(stmt):
                if defs:
                    qderive[k].update(defs)
                else:
                    qderive[k].add("<ALL>")
            for u in names_in(stmt):
                if defs:
                    for d in defs:
                        if d != u:
                            derive[u].add(d)
                else:
                    import_use.add(u)


def visit_body(body, fname):
    ls = line_sets.get(fname, {})
    for stmt in body:
        for n, k in qual_keys(stmt):
            s = ls.get(n.lineno)
            if s is None:
                continue
            if s is ALL:
                qrefs_all.add(k)
            else:
                qrefs[k] |= s
        for n in ast.walk(stmt):
            if isinstance(n, (ast.Name, ast.Attribute)) and hasattr(n, "lineno"):
                nm = n.id if isinstance(n, ast.Name) else n.attr
                s = ls.get(n.lineno)
                if s is None:
                    continue
                if s is ALL:
                    refs_all.add(nm)
                else:
                    refs[nm] |= s


for top in ("sqlglot", "tests"):
    for dp, _, fs in os.walk(os.path.join(src, top)):
        for fn in fs:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            try:
                tree = ast.parse(open(p, encoding="utf-8").read())
            except SyntaxError:
                continue
            visit_level(tree.body, rel)

# Classes each test used (type of self/cls in sqlglot calls, with its bases).
classes_out = {}
classes_import = set()
cpath = os.path.join(raw, "classes.json")
if os.path.exists(cpath):
    for key, ctxs in json.load(open(cpath)).items():
        acc = set()
        for c in ctxs:
            if c == "<import>":
                classes_import.add(key)  # used while a sqlglot module body ran
                continue
            if c.startswith("C|"):
                acc |= set(by_file.get(c[2:], []))  # a test module's own top-level code
                continue
            t = translate(c)
            if t is ALL:
                acc = ALL
                break
            acc |= t
        classes_out[key] = sid(acc)

# Class-body statements that use a name together with other (class) names, so a
# change to Snowflake.X also reaches a class whose body copies Snowflake.X.
class_uses = defaultdict(list)


def visit_classes(body, rel, prefix):
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            q = prefix + [stmt.name]
            key = rel + "::" + ".".join(q)
            for inner in stmt.body:
                if isinstance(inner, ast.ClassDef):
                    continue
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    parts = inner.decorator_list + inner.args.defaults
                else:
                    parts = [inner]
                used = {u for part in parts for u in names_in(part)}
                caps = sorted(u for u in used if u[:1].isupper())
                for u in used:
                    class_uses[u].append([key, caps])
            visit_classes(stmt.body, rel, q)


for top in ("sqlglot",):
    for dp, _, fs in os.walk(os.path.join(src, top)):
        for fn in fs:
            if fn.endswith(".py"):
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, src).replace(os.sep, "/")
                try:
                    visit_classes(ast.parse(open(p, encoding="utf-8").read()).body, rel, [])
                except SyntaxError:
                    pass

# Identifiers that also appear as string constants (getattr/setattr/settings
# lists): reads of these can bypass attribute syntax, so never narrow by them.
str_names = set()
for top in ("sqlglot", "tests"):
    for dp, _, fs in os.walk(os.path.join(src, top)):
        for fn in fs:
            if fn.endswith(".py"):
                try:
                    tree = ast.parse(open(os.path.join(dp, fn), encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                        str_names.add(n.value)

# Comparisons where `a is b` and `a == b` disagreed, per test (missing = never).
cmp_out, cmp_seen = {}, []
import glob
merged_differ, merged_seen = defaultdict(set), set()
for cmp_path in glob.glob(os.path.join(raw, "cmp_*.json")):  # one file per pytest-xdist worker
    cd = json.load(open(cmp_path))
    merged_seen.update(cd["seen"])
    for key, ctxs in cd["differ"].items():
        merged_differ[key].update(ctxs)
if merged_seen:
    cmp_seen = sorted(merged_seen)
    for key, ctxs in merged_differ.items():
        acc = set()
        for c in ctxs:
            t = translate(c)
            if t is ALL:
                acc = ALL
                break
            acc |= t
        cmp_out[key] = sid(acc)

# Tests that share one process pool kept on the test class (self.pool / cls.pool):
# a patch that crashes a pool worker breaks the pool for every later user, so they
# fail together. Found as the tests that ran a `self.pool.submit(` line.
pool_by_class = defaultdict(set)
for f, ls in line_sets.items():
    if not f.startswith("tests/"):
        continue
    try:
        text = open(os.path.join(src, f), encoding="utf-8").read().splitlines()
    except OSError:
        continue
    for ln, s in ls.items():
        if s is not ALL and 0 < ln <= len(text) and re.search(r"(self|cls)\.\w*pool\w*\.(submit|map)\(", text[ln - 1]):
            for i in s:  # group by the test class that owns the pool
                pool_by_class[tests[i].rsplit("::", 1)[0]].add(i)
pool_groups = [sid(g) for g in pool_by_class.values()]

refs_out = {n: sid(s) for n, s in refs.items()}
for n in refs_all:
    refs_out[n] = -1
data = {
    "tests": tests,
    "sets": sets,
    "lines": lines,
    "opens": opens_out,
    "refs": refs_out,
    "derive": {k: sorted(v) for k, v in derive.items()},
    "import_use": sorted(import_use),
    "classes": classes_out,
    "classes_import": sorted(classes_import),
    "qrefs": {**{k: sid(v) for k, v in qrefs.items()}, **{k: -1 for k in qrefs_all}},
    "qderive": {k: sorted(v) for k, v in qderive.items()},
    "str_names": sorted(str_names),
    "cmp_differ": cmp_out,
    "pool_groups": pool_groups,
    "cmp_seen": cmp_seen,
    "class_uses": class_uses,
}
with gzip.open(out, "wt", encoding="utf-8") as f:
    json.dump(data, f, separators=(",", ":"))
print(f"tests {len(tests)} sets {len(sets)} files {len(lines)} opens {len(opens_out)} refs {len(refs_out)}"
      f" -> {os.path.getsize(out)/1e6:.2f} MB", file=sys.stderr)
