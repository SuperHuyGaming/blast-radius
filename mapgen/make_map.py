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
import ast, gzip, json, os, sys
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


def translate(ctx):
    """Context name -> set of test indexes, or None for every test."""
    if ctx == "<import>":
        return ALL
    kind, rest = ctx.split("|", 1)
    if kind == "C":
        return set(by_file.get(rest, [])) if rest.startswith("tests/") else ALL
    if rest not in tindex:
        return set()
    if kind == "R":
        return {tindex[rest]}
    return set(by_class[rest.rsplit("::", 1)[0]])


ctx_sets = [translate(c) for c in cov["contexts"]]
sets, set_ids = [], {}


def sid(s):
    if s is ALL:
        return -1
    key = tuple(sorted(s))
    if key not in set_ids:
        set_ids[key] = len(sets)
        sets.append(list(key))
    return set_ids[key]


def union(ctx_idx):
    acc = set()
    for c in ctx_idx:
        s = ctx_sets[c]
        if s is ALL:
            return ALL
        acc |= s
    return acc


lines = {}
line_sets = {}
for f, lm in cov["files"].items():
    line_sets[f] = {int(l): union(cs) for l, cs in lm.items()}
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
cpath = os.path.join(raw, "classes.json")
if os.path.exists(cpath):
    for key, ctxs in json.load(open(cpath)).items():
        acc = set()
        for c in ctxs:
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
    "class_uses": class_uses,
}
with gzip.open(out, "wt", encoding="utf-8") as f:
    json.dump(data, f, separators=(",", ":"))
print(f"tests {len(tests)} sets {len(sets)} files {len(lines)} opens {len(opens_out)} refs {len(refs_out)}"
      f" -> {os.path.getsize(out)/1e6:.2f} MB", file=sys.stderr)
