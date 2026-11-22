"""Build maps/clsmap.json.gz from a clsrec recording run.

usage: python clsbuild.py <rec dir> <tests.json> <sqlglot tree> [out]
"""

import ast
import glob
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import clsmap as C  # noqa: E402


def load_workers(rec):
    ws = []
    for p in sorted(glob.glob(os.path.join(rec, "rec-*.json"))):
        with open(p) as f:
            d = json.load(f)
        if d["tests"] or "imp" in d:
            ws.append((os.path.basename(p), d))
    # under xdist the controller (rec-main.json) lists every test but ran none, and its
    # descriptor table is its own import of sqlglot, not the workers': drop it when workers exist
    others = [w for w in ws if w[0] != "rec-main.json"]
    return others or ws


def desc_name(d):
    m, q, a, _ = d
    return "%s:%s.%s" % (m, q, a) if q else "%s:%s" % (m, a)


class Src:
    """Parsed sqlglot sources: scopes, module-level definitions, code objects."""

    def __init__(self, tree):
        self.tree = tree
        self.files = {}
        for p in glob.glob(os.path.join(tree, "sqlglot", "**", "*.py"), recursive=True):
            rel = os.path.relpath(p, tree)
            self.files[C.module_of(rel)] = rel
        self._ast = {}
        self._scopes = {}
        self._defs = {}
        self.decorated = set()  # module:name of module-level defs carrying a decorator
        for mod in list(self.files):
            self.defs(mod)

    def ast(self, mod):
        if mod not in self._ast:
            rel = self.files.get(mod)
            if rel is None:
                self._ast[mod] = None
            else:
                with open(os.path.join(self.tree, rel)) as f:
                    self._ast[mod] = ast.parse(f.read())
        return self._ast[mod]

    def scopes(self, mod):
        if mod not in self._scopes:
            t = self.ast(mod)
            self._scopes[mod] = C.Scopes(t) if t is not None else None
        return self._scopes[mod]

    def defs(self, mod):
        """name -> 'assign' | 'def' | 'class' | ('import', mod, name) | 'module'"""
        if mod in self._defs:
            return self._defs[mod]
        out = self._defs[mod] = {}  # set before the walk: star imports can be circular
        t = self.ast(mod)
        if t is not None:
            pkg = mod if self.files.get(mod, "").endswith("__init__.py") else mod.rpartition(".")[0]
            for st in C._flat(t.body):
                if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                    for nm in C._target_names(tg):
                        if not nm.startswith(("[", ".", "?")):
                            out.setdefault(nm, "assign")
                elif isinstance(st, C.FUNCS):
                    out.setdefault(st.name, "def")
                    if st.decorator_list:
                        self.decorated.add("%s:%s" % (mod, st.name))
                elif isinstance(st, ast.ClassDef):
                    out.setdefault(st.name, "class")
                elif isinstance(st, ast.ImportFrom):
                    base = st.module or ""
                    if st.level:
                        parts = pkg.split(".")
                        if st.level > 1:
                            parts = parts[: len(parts) - st.level + 1]
                        base = ".".join(parts + ([st.module] if st.module else []))
                    for a in st.names:
                        if a.name == "*":
                            # every name of the source module (a superset of what * binds)
                            if base in self.files and base != mod:
                                for nm in list(self.defs(base)):
                                    out.setdefault(nm, ("import", base, nm))
                            continue
                        out.setdefault(a.asname or a.name, ("import", base, a.name))
                elif isinstance(st, ast.Import):
                    for a in st.names:
                        out.setdefault(a.asname or a.name.split(".")[0], "module")
        return out

    def resolve_global(self, mod, name, depth=0):
        """Origin 'module:name' of a module-level assigned value or decorated def, or None."""
        if depth > 10:
            return None
        d = self.defs(mod).get(name)
        if d == "assign" or (d == "def" and "%s:%s" % (mod, name) in self.decorated):
            return "%s:%s" % (mod, name)
        if isinstance(d, tuple):
            src, nm = d[1], d[2]
            if src.startswith("sqlglot"):
                if src + "." + nm in self.files:
                    return None
                return self.resolve_global(src, nm, depth + 1)
        return None

    def module_alias(self, mod, name):
        d = self.defs(mod).get(name)
        if isinstance(d, tuple) and d[1].startswith("sqlglot") and (d[1] + "." + d[2]) in self.files:
            return d[1] + "." + d[2]
        if d == "module" and name == "sqlglot":
            return "sqlglot"
        return None


def _kp_names(stmt):
    """Names (terminal) used key-preservingly: **X, *X, X.copy(), X | .., X - .., dict(X), set(X)."""
    out = set()

    def term(n):
        if isinstance(n, ast.Name):
            return n.id
        if isinstance(n, ast.Attribute):
            return n.attr
        return None

    for node in ast.walk(stmt):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if k is None and term(v):
                    out.add(term(v))
        elif isinstance(node, ast.Starred) and term(node.value):
            out.add(term(node.value))
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "copy" and term(f.value):
                out.add(term(f.value))
            if isinstance(f, ast.Name) and f.id in ("dict", "set", "frozenset") and node.args and term(node.args[0]):
                out.add(term(node.args[0]))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitOr, ast.Sub, ast.BitAnd)):
            for side in (node.left, node.right):
                if term(side):
                    out.add(term(side))
    return out


def _loads(stmt):
    """Name loads and Attribute nodes outside lambda bodies, excluding comprehension-bound names."""
    names = []
    attrs = []

    def visit(n, bound):
        if isinstance(n, ast.Lambda):
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x is not None]:
                visit(d, bound)
            return
        if isinstance(n, C.FUNCS):
            for d in n.decorator_list + n.args.defaults + [x for x in n.args.kw_defaults if x is not None]:
                visit(d, bound)
            return
        if isinstance(n, ast.ClassDef):
            for d in n.decorator_list + n.bases + [k.value for k in n.keywords]:
                visit(d, bound)
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            b = set(bound)
            for g in n.generators:
                visit(g.iter, b)
                for t in ast.walk(g.target):
                    if isinstance(t, ast.Name):
                        b.add(t.id)
                for i in g.ifs:
                    visit(i, b)
            for f in ("elt", "key", "value"):
                if hasattr(n, f):
                    visit(getattr(n, f), b)
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in bound:
                names.append(n.id)
            return
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            attrs.append((n.value.id, n.attr))
        for c in ast.iter_child_nodes(n):
            visit(c, bound)

    visit(stmt, set())
    return names, attrs


class Builder:
    def __init__(self, rec, tests_json, tree):
        self.src = Src(tree)
        with open(tests_json) as f:
            tj = json.load(f)
        self.num = {t["id"]: t["number"] for t in tj["tests"]}
        self.ntests = len(tj["tests"])
        self.by_class = {}
        self.by_file = {}
        for t in tj["tests"]:
            self.by_class.setdefault(t["id"].rpartition("::")[0], []).append(t["number"])
            self.by_file.setdefault(t["file"], []).append(t["number"])
        self.workers = load_workers(rec)
        ref = None
        for name, d in self.workers:
            if ref is None:
                ref = d["descs"]
            elif d["descs"] != ref:
                raise SystemExit("descriptor tables differ across workers: %s" % name)
        self.descs_raw = ref
        self.names = [desc_name(d) for d in ref]
        self.reads = {}  # desc name -> filt -> set
        self.vtype = {}
        for i, d in enumerate(ref):
            n = self.names[i]
            vt = d[3]
            if vt.startswith("alias:"):
                vt = "alias:" + self.names[int(vt[6:])]
            old = self.vtype.get(n)
            if old is not None and old != vt:
                vt = "A"  # merged homonyms: most conservative
            self.vtype[n] = vt
            self.reads.setdefault(n, {})
        self.O = {}
        self.F = {}
        self.edges = {}
        self.gedges = {}
        self.mro = {}
        self.comps = {}
        self.unknown_tests = set()

    def _expand(self, bucket, idx, w):
        if bucket == "call":
            nid = w["tests"][idx]
            n = self.num.get(nid)
            if n is None:
                self.unknown_tests.add(nid)
                return []
            return [n]
        if bucket == "setup":
            nid = w["tests"][idx]
            return self.by_class.get(nid.rpartition("::")[0], [])
        f = w["collect_files"][idx]
        return self.by_file.get(f, [])

    def merge(self):
        for _, w in self.workers:
            for bucket in ("call", "setup", "collect"):
                b = w[bucket]
                cache = {}

                def nums(idxs):
                    out = set()
                    for i in idxs:
                        r = cache.get(i)
                        if r is None:
                            r = cache[i] = self._expand(bucket, i, w)
                        out.update(r)
                    return out

                for i, idxs in b["R"].items():
                    self.reads[self.names[int(i)]].setdefault("R", set()).update(nums(idxs))
                for i, kind, key, idxs in b["KK"]:
                    filt = kind if key is None else kind + ":" + key
                    self.reads[self.names[i]].setdefault(filt, set()).update(nums(idxs))
                for t, idxs in b["O"].items():
                    self.O.setdefault(t, set()).update(nums(idxs))
                for c, idxs in b["F"].items():
                    self.F.setdefault(c, set()).update(nums(idxs))
            for c, bases in w["mro"].items():
                self.mro[c] = bases
            for d, cs in w.get("comps", {}).items():
                self.comps[d] = cs

    # ---- users ----
    def users(self):
        own = {c: set(s) for c, s in self.O.items()}
        classes_by_mod = {}
        for c in self.mro:
            m, _, q = c.partition(":")
            classes_by_mod.setdefault(m, set()).add(q)
        for fk, s in self.F.items():
            rel, qual, _ = fk.rsplit(":", 2)
            mod = C.module_of(rel)
            parts = qual.split(".")
            known = classes_by_mod.get(mod, ())
            cls = None
            for k in range(len(parts) - 1, 0, -1):
                if ".".join(parts[:k]) in known:
                    cls = ".".join(parts[:k])
                    break
            if cls:
                own.setdefault(mod + ":" + cls, set()).update(s)
        users = {c: set(s) for c, s in own.items()}
        for c, s in own.items():
            for b in self.mro.get(c, ()):
                users.setdefault(b, set()).update(s)
        return users

    # ---- import-time edges ----
    def _stmt_dst(self, mod, qual, st, key):
        """(dtype, dst) for what a scope statement defines."""
        rel = self.src.files.get(mod)
        if st is None:
            return ("users", [mod + ":" + qual]) if qual else ("all", None)
        if key[0] == "a":
            out = []
            for nm in key[1]:
                if nm.startswith("[]"):
                    nm = nm[2:]
                elif nm.startswith((".", "?")):
                    return ("all", None)
                out.append(nm)
            if qual:
                return ("attrs", ["%s:%s.%s" % (mod, qual, nm) for nm in out])
            return ("globals", ["%s:%s" % (mod, nm) for nm in out])
        if key[0] == "f":
            q = (qual + "." if qual else "") + st.name
            return ("func", "%s:%s:%d" % (rel, q, C._start(st)))
        if key[0] == "c":
            q = (qual + "." if qual else "") + st.name
            return ("users", [mod + ":" + q])
        if key[0] == "call" and key[1] != "?":
            nm = key[1].split(".")[0]
            if qual:
                return ("attrs", ["%s:%s.%s" % (mod, qual, nm)])
            return ("globals", ["%s:%s" % (mod, nm)])
        if key[0] == "doc":
            return ("none", None)
        if qual:
            return ("users", [mod + ":" + qual])
        return ("all", None)

    def _add_edge(self, src, filt, dtype, dst, kp):
        if dtype == "none":
            return
        if dtype == "attrs":
            for d in dst:
                self._put(src, [filt, "attr", d, kp])
        elif dtype == "globals":
            for d in dst:
                self._put(src, [filt, "global", d, kp])
        else:
            self._put(src, [filt, dtype, dst, kp])

    def _put(self, src, e):
        if src in self.reads:
            lst = self.edges.setdefault(src, [])
        else:
            lst = self.gedges.setdefault(src, [])
        if e not in lst:
            lst.append(e)

    def code_names(self):
        """cname -> (module, co_names incl. nested code objects without their own record)."""
        out = {}
        for mod, rel in self.src.files.items():
            with open(os.path.join(self.src.tree, rel)) as f:
                try:
                    code = compile(f.read(), rel, "exec")
                except SyntaxError:
                    continue
            stack = [code]
            while stack:
                c = stack.pop()
                # module bodies and the class bodies in them (no CO_OPTIMIZED) get no names:
                # static_edges reads their statements one by one, each with its own target
                scope = not c.co_flags & 0x1 and "<locals>" not in c.co_qualname
                names = set() if scope else set(c.co_names)
                out["%s:%s:%d" % (rel, c.co_qualname, c.co_firstlineno)] = (mod, names)
                for k in c.co_consts:
                    if hasattr(k, "co_code"):
                        stack.append(k)
        return out

    def import_edges(self, cnames):
        imp = None
        for _, w in self.workers:
            if "imp" in w:
                imp = w["imp"]
                break
        if imp is None:
            raise SystemExit("no import records")
        unresolved = 0
        by_bare = self.by_bare()
        for ctx, recs in imp:
            t = ctx[0]
            if t == "body":
                _, mod, qual, line = ctx
                sc = self.src.scopes(mod)
                loc = sc._locate(qual, line) if sc else None
                if loc is None or loc[0] != qual:
                    if loc is not None:
                        qual2, key, st, _ = loc
                        dtype, dst = self._stmt_dst(mod, qual2, st, key)
                    else:
                        dtype, dst = "users", [mod + ":" + qual]
                        unresolved += 1
                    kpn = set()
                else:
                    _, key, st, _ = loc
                    dtype, dst = self._stmt_dst(mod, qual, st, key)
                    kpn = _kp_names(st)
            elif t == "post":
                _, mod, qual = ctx
                dtype, dst, kpn = "users", [mod + ":" + qual], set()
            elif t == "mod":
                _, mod, line = ctx
                sc = self.src.scopes(mod)
                loc = sc._locate("", line) if sc else None
                if loc is None:
                    dtype, dst, kpn = "all", None, set()
                    unresolved += 1
                else:
                    qual2, key, st, _ = loc
                    dtype, dst = self._stmt_dst(mod, qual2, st, key)
                    kpn = _kp_names(st)
            else:
                continue
            for r in recs:
                if r[0] == "F":
                    info = cnames.get(r[1])
                    if info is None:
                        continue
                    cmod, names = info
                    for nm in names:
                        # by bare name too: `module.NAME` reads leave only the attribute name
                        targets = set(by_bare.get(nm, ()))
                        g = self.src.resolve_global(cmod, nm)
                        if g is not None:
                            targets.add(g)
                        for g in sorted(targets):
                            self._add_edge(g, "R", dtype, dst, False)
                    continue
                src = self.names[r[0]]
                filt = "R" if len(r) == 1 else (r[1] if r[2] is None else r[1] + ":" + r[2])
                ddst = dst
                if t == "post":
                    sc_cls = src.rpartition(".")[0]
                    ddst = [mod + ":" + qual, sc_cls]
                kp = src.rpartition(".")[2].rpartition(":")[2] in kpn
                self._add_edge(src, filt, dtype, ddst, kp)
        return unresolved

    def static_edges(self):
        for mod in list(self.src.files):
            sc = self.src.scopes(mod)
            if sc is None:
                continue
            for qual, stmts in sc.stmts.items():
                earlier = set()
                for key, st in stmts.items():
                    dtype, dst = self._stmt_dst(mod, qual, st, key)
                    kpn = _kp_names(st)
                    if isinstance(st, ast.ClassDef):
                        nodes = st.decorator_list + st.bases + [k.value for k in st.keywords]
                    else:
                        nodes = [st]
                    for node in nodes:
                        names, attrs = _loads(node)
                        for nm in names:
                            if qual and nm in earlier:
                                src = "%s:%s.%s" % (mod, qual, nm)
                            else:
                                src = self.src.resolve_global(mod, nm)
                            if src is not None:
                                self._add_edge(src, "R", dtype, dst, nm in kpn)
                        for base, at in attrs:
                            m2 = self.src.module_alias(mod, base)
                            if m2 is not None:
                                src = self.src.resolve_global(m2, at)
                                if src is not None:
                                    self._add_edge(src, "R", dtype, dst, at in kpn)
                    if key[0] == "a":
                        for nm in key[1]:
                            earlier.add(nm)

    def by_bare(self):
        """bare name -> every module global (assigned value or decorated def) of that name."""
        out = {}
        for mod in self.src.files:
            for nm, d in self.src.defs(mod).items():
                if d == "assign" or (d == "def" and "%s:%s" % (mod, nm) in self.src.decorated):
                    out.setdefault(nm, []).append("%s:%s" % (mod, nm))
        return out

    def globals_map(self, cnames):
        """module global -> tests running code that names it (LOAD_GLOBAL or attribute)."""
        by_bare = self.by_bare()
        G = {}
        for fk, tests in self.F.items():
            info = cnames.get(fk)
            if info is None:
                continue
            cmod, names = info
            for nm in names:
                targets = set(by_bare.get(nm, ()))
                g = self.src.resolve_global(cmod, nm)
                if g is not None:
                    targets.add(g)
                for g in targets:
                    G.setdefault(g, set()).update(tests)
        return G

    def test_words(self):
        """bare name of a module global -> tests whose test file mentions it (test code is not
        in F). A tests/ file with no test of its own is a helper: every test."""
        import re

        names = set(self.by_bare())
        out = {}
        pat = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
        for p in glob.glob(os.path.join(self.src.tree, "tests", "**", "*.py"), recursive=True):
            rel = os.path.relpath(p, self.src.tree)
            with open(p, encoding="utf-8", errors="replace") as f:
                words = set(pat.findall(f.read())) & names
            tests = self.by_file.get(rel)
            if tests is None:
                tests = range(self.ntests)
            for w in words:
                out.setdefault(w, set()).update(tests)
        return out

    def post_names(self):
        """Attribute names read by metaclasses and __init_subclass__ hooks."""
        out = set()
        for mod in self.src.files:
            t = self.src.ast(mod)
            if t is None:
                continue
            for node in ast.walk(t):
                if isinstance(node, ast.ClassDef):
                    is_meta = any(isinstance(b, ast.Name) and b.id == "type" for b in node.bases)
                    for st in node.body:
                        if isinstance(st, C.FUNCS) and (
                            st.name == "__init_subclass__" or (is_meta and st.name in ("__new__", "__init__"))
                        ):
                            for n in ast.walk(st):
                                if isinstance(n, ast.Attribute):
                                    out.add(n.attr)
                                elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                                    out.add(n.value)
        return out

    def build(self, out):
        self.merge()
        cnames = self.code_names()
        unresolved = self.import_edges(cnames)
        self.static_edges()
        users = self.users()
        G = self.globals_map(cnames)
        sets = []
        index = {}

        def sid(s):
            k = tuple(sorted(s))
            i = index.get(k)
            if i is None:
                i = index[k] = len(sets)
                sets.append(list(k))
            return i

        descs = {}
        for n, rd in self.reads.items():
            descs[n] = [
                self.vtype[n],
                {f: sid(s) for f, s in rd.items() if s},
                self.edges.get(n, []),
            ]
        comps_rev = {}
        for d, cs in self.comps.items():
            for c in cs:
                comps_rev.setdefault(c, []).append(d)
        m = {
            "sets": sets,
            "descs": descs,
            "desc_names": [],
            "users": {c: sid(s) for c, s in users.items() if s},
            "comps_rev": comps_rev,
            "G": {g: sid(s) for g, s in G.items() if s},
            "TW": {w: sid(s) for w, s in self.test_words().items() if s},
            "gedges": self.gedges,
            "F": {f: sid(s) for f, s in self.F.items() if s},
            "mro": self.mro,
            "post_names": sorted(self.post_names()),
        }
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with gzip.open(out, "wt", compresslevel=9) as f:
            json.dump(m, f, separators=(",", ":"))
        print(
            "descs=%d sets=%d users=%d G=%d F=%d edges=%d gedges=%d unresolved_ctx=%d unknown_tests=%d"
            % (
                len(descs),
                len(sets),
                len(m["users"]),
                len(m["G"]),
                len(m["F"]),
                sum(len(e) for e in self.edges.values()),
                sum(len(e) for e in self.gedges.values()),
                unresolved,
                len(self.unknown_tests),
            ),
            file=sys.stderr,
        )


if __name__ == "__main__":
    rec, tj, tree = sys.argv[1:4]
    out = sys.argv[4] if len(sys.argv) > 4 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "maps", "clsmap.json.gz")
    Builder(rec, tj, tree).build(out)
