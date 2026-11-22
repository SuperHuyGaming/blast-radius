"""Builds maps/cov.bin from a per-test coverage run of sqlglot at eb11aec.

usage: build_maps.py <sqlglot checkout> <cov out dir> <tests.json> <out file> [iso.json]
cov out dir holds .coverage.* (contexts = pytest node ids) and opens.*.json.
iso.json (tools/iso/isorun.py) adds "need": per sqlglot module, the tests that need it.
"""

import base64
import glob
import json
import os
import sys
import zlib

import coverage


def code_lines(src, path):
    """Lines that carry bytecode, from every code object in the module."""
    out = set()
    try:
        co = compile(src, path, "exec")
    except SyntaxError:
        return out
    stack = [co]
    while stack:
        c = stack.pop()
        for _, _, ln in c.co_lines():
            if ln is not None:
                out.add(ln)
        for k in c.co_consts:
            if hasattr(k, "co_lines"):
                stack.append(k)
    return out


def body_lines(src, path):
    """Lines run by module and class bodies (code without CO_OPTIMIZED)."""
    out = set()
    try:
        co = compile(src, path, "exec")
    except SyntaxError:
        return out
    stack = [co]
    while stack:
        c = stack.pop()
        if not c.co_flags & 0x1:
            for _, _, ln in c.co_lines():
                if ln is not None:
                    out.add(ln)
        for k in c.co_consts:
            if hasattr(k, "co_lines"):
                stack.append(k)
    return out


def top_imports(tree, mod, is_pkg):
    """Modules named by the import statements a module runs when it is imported: its body
    and class bodies, not function bodies and not `if TYPE_CHECKING:` blocks."""
    import ast

    out = set()

    def walk(body):
        for st in body:
            if isinstance(st, ast.Import):
                out.update(a.name for a in st.names)
            elif isinstance(st, ast.ImportFrom):
                base = st.module or ""
                if st.level:
                    pkg = mod.split(".") if is_pkg else mod.split(".")[:-1]
                    pkg = pkg[: len(pkg) - st.level + 1]
                    base = ".".join(pkg + ([base] if base else []))
                out.add(base)
                out.update(base + "." + a.name for a in st.names)
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            else:
                test = getattr(st, "test", None)
                name = getattr(test, "attr", None) or getattr(test, "id", None)
                if not (isinstance(st, ast.If) and name == "TYPE_CHECKING"):
                    walk(getattr(st, "body", ()))
                for field in ("orelse", "finalbody"):
                    walk(getattr(st, field, ()))
                for h in getattr(st, "handlers", ()):
                    walk(h.body)

    walk(tree.body)
    return out


def main():
    root, covdir, tests_json, out = sys.argv[1:5]
    iso_path = sys.argv[5] if len(sys.argv) > 5 else None
    root = os.path.realpath(root)
    tests = json.load(open(tests_json))["tests"]
    ids = [t["id"] for t in tests]
    index = {i: n for n, i in enumerate(ids)}
    unknown = set()

    def bit_of(ctx):
        n = index.get(ctx)
        if n is None:
            # parametrized/subtest variants are not separate tests in tests.json
            base = ctx.split("[", 1)[0]
            n = index.get(base)
        if n is None and ctx:
            unknown.add(ctx)
        return n

    ALL = (1 << len(ids)) - 1
    scope_cache = {}

    def ctx_bits(ctx):
        """Bits for a coverage/opens context: a test, or a fixture scope."""
        if ctx.startswith("@scope:"):
            b = scope_cache.get(ctx)
            if b is None:
                _, scope, node = ctx.split(":", 2)
                b = 0
                if scope in ("class", "module"):
                    for n, i in enumerate(ids):
                        if i == node or i.startswith(node + "::"):
                            b |= 1 << n
                if not b:
                    b = ALL
                scope_cache[ctx] = b
            return b
        n = bit_of(ctx)
        return None if n is None else 1 << n

    sets: dict[int, int] = {}
    set_list: list[int] = []

    def intern(bits):
        s = sets.get(bits)
        if s is None:
            s = len(set_list)
            sets[bits] = s
            set_list.append(bits)
        return s

    files = {}
    per_file_lines: dict[str, dict[int, int]] = {}
    imp_lines: dict[str, dict[str, set]] = {}  # module -> file -> lines run while importing it
    for dfile in sorted(glob.glob(os.path.join(covdir, ".coverage.*"))):
        data = coverage.CoverageData(basename=dfile)
        data.read()
        for f in data.measured_files():
            rel = os.path.relpath(os.path.realpath(f), root)
            if rel.startswith(".."):
                continue
            d = per_file_lines.setdefault(rel, {})
            for ln, ctxs in data.contexts_by_lineno(f).items():
                b = 0
                for c in ctxs:
                    if c.startswith("@import:"):
                        imp_lines.setdefault(c[len("@import:"):], {}).setdefault(rel, set()).add(ln)
                        continue
                    cb = ctx_bits(c)
                    if cb is not None:
                        b |= cb
                if b:
                    d[ln] = d.get(ln, 0) | b
    reads = {}
    imports = {}
    lists = {}
    pending = []
    for of in glob.glob(os.path.join(covdir, "opens.*.json")):
        for ctx, paths in json.load(open(of)).items():
            if ctx.startswith("@file:"):
                f = ctx[len("@file:"):]
                b = 0
                for n, t in enumerate(tests):
                    if t["file"] == f:
                        b |= 1 << n
                if not b:
                    pending.append((f, paths))
                    continue
            else:
                b = ctx_bits(ctx)
                if b is None:
                    continue
            for rel, how in paths.items():
                tgt = reads if how == "read" else lists if how == "list" else imports
                tgt[rel] = tgt.get(rel, 0) | b
    # tests/ import closure: for each tests/ module, test files that import it
    # (directly or through other tests/ modules)
    import ast as _ast
    tmods = {}
    for p in glob.glob(os.path.join(root, "tests", "**", "*.py"), recursive=True):
        rel = os.path.relpath(p, root)
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        deps = set()
        try:
            tree = _ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    deps.add(a.name)
            elif isinstance(node, _ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    pkg = mod.split(".")[: -node.level] if not rel.endswith("__init__.py") else mod.split(".")[: len(mod.split(".")) - node.level + 1]
                    base = ".".join(pkg + ([base] if base else []))
                deps.add(base)
                for a in node.names:
                    deps.add(base + "." + a.name)
        tmods[mod] = (rel, deps)
    mod_of = {m: r for m, (r, _) in tmods.items()}
    importers = {r: {r} for r, _ in tmods.values()}
    changed = True
    while changed:
        changed = False
        for m, (rel, deps) in tmods.items():
            for d in deps:
                # importing a.b.c imports a and a.b too
                parts = d.split(".")
                for k in range(1, len(parts) + 1):
                    dm = ".".join(parts[:k])
                    if dm in mod_of:
                        tgt = importers[mod_of[dm]]
                        add = importers[rel] - tgt
                        if add:
                            tgt |= add
                            changed = True
    by_file = {}
    for n, t in enumerate(tests):
        by_file[t["file"]] = by_file.get(t["file"], 0) | (1 << n)
    test_closure = {}
    closure_bits = {}
    for rel, imps in importers.items():
        b = 0
        for f in imps:
            b |= by_file.get(f, 0)
        if b:
            closure_bits[rel] = b
            test_closure[rel] = intern(b)
    for f, paths in pending:
        b = closure_bits.get(f, (1 << len(ids)) - 1)
        for rel, how in paths.items():
            tgt = reads if how == "read" else lists if how == "list" else imports
            tgt[rel] = tgt.get(rel, 0) | b
    # Lines run while a sqlglot module is imported serve every test that may be
    # first to import it: tests touching the module or anything importing it
    # (static imports anywhere in sqlglot/, plus edges seen at run time), and
    # the test files that import any of those.
    smods = {}
    for p in glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True):
        rel = os.path.relpath(p, root)
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        smods[mod] = rel
    imp_by = {m: set() for m in smods}  # module -> modules importing it

    def add_edge(dep, importer):
        parts = dep.split(".")
        for k in range(1, len(parts) + 1):
            dm = ".".join(parts[:k])
            if dm in imp_by and dm != importer:
                imp_by[dm].add(importer)

    for mod, rel in smods.items():
        try:
            tree = _ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
        except SyntaxError:
            continue
        is_pkg = rel.endswith("__init__.py")
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    add_edge(a.name, mod)
            elif isinstance(node, _ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    pkg = mod.split(".") if is_pkg else mod.split(".")[:-1]
                    pkg = pkg[: len(pkg) - node.level + 1]
                    base = ".".join(pkg + ([base] if base else []))
                add_edge(base, mod)
                for a in node.names:
                    add_edge(base + "." + a.name, mod)
    for ef in glob.glob(os.path.join(covdir, "edges.*.json")):
        for mod, imps in json.load(open(ef)).items():
            for i in imps:
                if i.startswith("@runtime:"):
                    i = i[len("@runtime:"):]
                if not i.startswith("@") and mod in imp_by:
                    imp_by[mod].add(i)
    touch = {}
    for rel, d in per_file_lines.items():
        b = 0
        for v in d.values():
            b |= v
        touch[rel] = b
    # test files importing a sqlglot module (through tests/ helpers too)
    timp = {}
    for m, (rel, deps) in tmods.items():
        tb = closure_bits.get(rel, 0)
        for dep in deps:
            parts = dep.split(".")
            for k in range(1, len(parts) + 1):
                dm = ".".join(parts[:k])
                if dm in smods:
                    timp[dm] = timp.get(dm, 0) | tb
    lazy = 0
    for n, i in enumerate(ids):
        if i.endswith("::test_lazy_load"):
            lazy |= 1 << n  # runs `import sqlglot` in a subprocess coverage can't see
    mod_bits = {}

    def import_bits(m):
        b = mod_bits.get(m)
        if b is not None:
            return b
        seen = {m}
        stack = [m]
        while stack:
            x = stack.pop()
            for y in imp_by.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        b = lazy
        for x in seen:
            b |= touch.get(smods.get(x, ""), 0) | timp.get(x, 0)
        mod_bits[m] = b
        return b

    # need[module file]: the tests a change anywhere in the module can reach.
    #   - tests that import the module when run alone in a fresh process (iso.json); a test
    #     with no clean record there counts for every module
    #   - tests that ran code of the module, or of a module whose import imports it
    #     (those build their own values from it), in the shared coverage run
    need = {}
    if iso_path:
        with open(iso_path) as f:
            iso = json.load(f)
        alone = {}
        norec = lazy
        for n, i in enumerate(ids):
            mods = iso["tests"].get(i)
            if mods is None or i in iso["rc"]:
                norec |= 1 << n
                continue
            for k in mods:
                alone[k] = alone.get(k, 0) | (1 << n)
        alone = {iso["mods"][k]: b for k, b in alone.items()}
        top_by = {m: set() for m in smods}
        for mod, rel in smods.items():
            try:
                tree = _ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
            except SyntaxError:
                continue
            for dep in top_imports(tree, mod, rel.endswith("__init__.py")):
                parts = dep.split(".")
                for k in range(1, len(parts) + 1):
                    dm = ".".join(parts[:k])
                    if dm in top_by and dm != mod:
                        top_by[dm].add(mod)
        for mod, rel in smods.items():
            seen = {mod}
            stack = [mod]
            while stack:
                for y in top_by[stack.pop()]:
                    if y not in seen:
                        seen.add(y)
                        stack.append(y)
            b = alone.get(mod, 0) | norec
            for x in seen:
                b |= touch.get(smods[x], 0)
            need[rel] = intern(b)
        print(f"need: modules={len(need)} tests_without_record={bin(norec).count('1')}", file=sys.stderr)

    # Only lines inside functions are spread: module and class bodies run once
    # at import by definition, and the selector reads them through clsmap.
    # A line shared by a body and a lambda counts as body.
    nimp = 0
    bl_cache = {}
    for m, per in imp_lines.items():
        b = import_bits(m) if m in smods else ALL
        for rel, lns in per.items():
            bl = bl_cache.get(rel)
            if bl is None:
                try:
                    bl = body_lines(open(os.path.join(root, rel), encoding="utf-8").read(), rel)
                except OSError:
                    bl = set()
                bl_cache[rel] = bl
            d = per_file_lines.setdefault(rel, {})
            for ln in lns:
                if ln in bl:
                    continue
                d[ln] = d.get(ln, 0) | b
                nimp += 1
    for rel in sorted(set(per_file_lines) | {
        os.path.relpath(p, root)
        for p in glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True)
        + glob.glob(os.path.join(root, "tests", "**", "*.py"), recursive=True)
    }):
        p = os.path.join(root, rel)
        try:
            src = open(p, encoding="utf-8").read()
        except OSError:
            continue
        d = per_file_lines.get(rel, {})
        files[rel] = {
            "exec": sorted(code_lines(src, p)),
            "lines": {str(ln): intern(b) for ln, b in sorted(d.items())},
        }
    raw = {
        "ntests": len(ids),
        "ids": ids,
        "sets": [base64.b64encode(b.to_bytes((len(ids) + 7) // 8, "little")).decode() for b in set_list],
        "files": files,
        "reads": {k: intern(v) for k, v in reads.items()},
        "imports_in_test": {k: intern(v) for k, v in imports.items()},
        "lists": {k: intern(v) for k, v in lists.items()},
        "test_closure": test_closure,
        "need": need,
    }
    # re-encode sets added by reads
    raw["sets"] = [base64.b64encode(b.to_bytes((len(ids) + 7) // 8, "little")).decode() for b in set_list]
    blob = zlib.compress(json.dumps(raw, separators=(",", ":")).encode(), 9)
    with open(out, "wb") as f:
        f.write(blob)
    print(f"import_lines={nimp} modules={len(imp_lines)}", file=sys.stderr)
    print(f"files={len(files)} sets={len(set_list)} reads={len(reads)} bytes={len(blob)} unknown_ctx={len(unknown)}", file=sys.stderr)
    for u in sorted(unknown)[:20]:
        print("  unknown ctx", u, file=sys.stderr)


if __name__ == "__main__":
    main()
