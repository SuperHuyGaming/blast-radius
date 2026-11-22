"""Tests affected by class-body and module-level changes in sqlglot (stdlib only).

affected(path, old_src, new_src, changed_old_lines, changed_new_lines) -> set[int] | None

Covers statements outside function and lambda bodies: class attributes (scalars, dict and set
literals per key), module-level assignments, def headers (decorators, defaults) and class
headers. Lines inside a def or lambda body are ignored: the caller covers them with line
coverage. None means the change can't be bounded here (imports, added/removed names and
classes, module-level control flow): it still reaches other code only through the changed
module, so the caller bounds it by the tests that need that module. ALL means the statement
writes into another module's objects: every test.

The map (maps/clsmap.json.gz) comes from clsbuild.py over a recording run (clsrec.py).
"""

import ast
import gzip
import json
import os

MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps", "clsmap.json.gz")
ALL = "ALL"

_M = None


def _map():
    global _M
    if _M is None:
        with gzip.open(MAP_PATH, "rt") as f:
            _M = json.load(f)
    return _M


def module_of(path):
    p = path[:-3] if path.endswith(".py") else path
    parts = p.split("/")
    if "sqlglot" in parts:
        parts = parts[parts.index("sqlglot") :]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# ---------- AST: statements of a scope ----------

COMPOUND = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
if hasattr(ast, "TryStar"):
    COMPOUND = COMPOUND + (ast.TryStar,)


def _start(node):
    decs = getattr(node, "decorator_list", None)
    if decs:
        return min(node.lineno, min(d.lineno for d in decs))
    return node.lineno


def _children_bodies(node):
    for f in ("body", "orelse", "finalbody"):
        b = getattr(node, f, None)
        if b:
            yield b
    for h in getattr(node, "handlers", ()) or ():
        yield h.body


def _flat(body):
    """Statements of a scope, compound statements flattened (their headers kept as entries)."""
    out = []
    for st in body:
        if isinstance(st, COMPOUND):
            out.append(st)
            for b in _children_bodies(st):
                out.extend(_flat(b))
        else:
            out.append(st)
    return out


def _stmt_key(st, counts):
    if isinstance(st, ast.Assign):
        names = tuple(sorted(_target_names(st.targets)))
        k = ("a", names)
    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        k = ("a", tuple(sorted(_target_names([st.target]))))
    elif isinstance(st, FUNCS):
        k = ("f", st.name)
    elif isinstance(st, ast.ClassDef):
        k = ("c", st.name)
    elif isinstance(st, COMPOUND):
        k = ("h", type(st).__name__)
    elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
        k = ("doc",)
    elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
        k = ("call", _call_target(st.value))
    else:
        k = ("x", type(st).__name__)
    n = counts.get(k, 0)
    counts[k] = n + 1
    return k + (n,)


def _target_names(targets):
    out = []
    for t in targets:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            out.extend(_target_names(t.elts))
        elif isinstance(t, ast.Starred):
            out.extend(_target_names([t.value]))
        elif isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
            out.append("[]" + t.value.id)
        elif isinstance(t, ast.Attribute):
            out.append("." + ast.unparse(t))
        else:
            out.append("?")
    return out


def _call_target(call):
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f.value.id + "." + f.attr
    return "?"


class Scopes:
    """Index of a module: scope qualname ('' = module) -> {key: stmt} and line lookup."""

    def __init__(self, tree):
        self.stmts = {}
        self.classes = {}
        self._index("", tree.body)

    def _index(self, qual, body):
        counts = {}
        d = {}
        for st in _flat(body):
            d[_stmt_key(st, counts)] = st
            if isinstance(st, ast.ClassDef):
                q = st.name if not qual else qual + "." + st.name
                self.classes[q] = st
                self._index(q, st.body)
        self.stmts[qual] = d

    def locate(self, line):
        """(scope, key, stmt, in_func_body) of the innermost scope statement holding line."""
        return self._locate("", line)

    def _locate(self, qual, line):
        best = None
        for k, st in self.stmts.get(qual, {}).items():
            if isinstance(st, COMPOUND):
                hdr_end = st.body[0].lineno - 1 if st.body else st.end_lineno
                if st.lineno <= line <= hdr_end:
                    best = (qual, k, st, False)
                continue
            if _start(st) <= line <= st.end_lineno:
                if isinstance(st, ast.ClassDef):
                    if st.body and line >= _start(st.body[0]):
                        q = st.name if not qual else qual + "." + st.name
                        inner = self._locate(q, line)
                        if inner is not None:
                            return inner
                        return None
                    return (qual, k, st, False)
                if isinstance(st, FUNCS):
                    in_body = bool(st.body) and line >= _start(st.body[0])
                    return (qual, k, st, in_body)
                return (qual, k, st, False)
        return best


# ---------- normalized dumps (lambda and def bodies are the caller's) ----------


def _norm(node):
    if node is None:
        return "None"
    if isinstance(node, ast.Lambda):
        return "Lambda(%s)" % ast.dump(node.args)
    if isinstance(node, FUNCS):
        return "Def(%s|%s|%s|%s)" % (
            node.name,
            ast.dump(node.args),
            "|".join(_norm(d) for d in node.decorator_list),
            type(node).__name__,
        )
    if isinstance(node, ast.ClassDef):
        return "Class(%s|%s|%s|%s)" % (
            node.name,
            "|".join(_norm(b) for b in node.bases),
            "|".join(_norm(k) for k in node.keywords),
            "|".join(_norm(d) for d in node.decorator_list),
        )
    if isinstance(node, COMPOUND):
        parts = [type(node).__name__]
        for f in ("test", "iter", "target"):
            if hasattr(node, f):
                parts.append(_norm(getattr(node, f)))
        for it in getattr(node, "items", ()) or ():
            parts.append(_norm(it.context_expr))
        for h in getattr(node, "handlers", ()) or ():
            parts.append(_norm(h.type) + str(h.name))
        return "|".join(parts)
    if isinstance(node, ast.AST):
        fields = []
        for f, v in ast.iter_fields(node):
            if isinstance(v, list):
                fields.append("[" + ",".join(_norm(x) for x in v) + "]")
            elif isinstance(v, ast.AST):
                fields.append(_norm(v))
            else:
                fields.append(repr(v))
        return type(node).__name__ + "(" + ",".join(fields) + ")"
    return repr(node)


# ---------- key representations (must match clsrec._kr) ----------


def _key_reprs(node):
    """Set of possible runtime key reprs for a dict key / set element expression, or None."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, str):
            return {v}
        if v is None or isinstance(v, (int, float, bool)):
            return {repr(v)}
        return None
    if isinstance(node, ast.Attribute):
        par = node.value
        out = {"<%s>" % node.attr}
        if isinstance(par, ast.Attribute):
            out.add("%s.%s" % (par.attr, node.attr))
        elif isinstance(par, ast.Name):
            out.add("%s.%s" % (par.id, node.attr))
        return out
    if isinstance(node, ast.Name):
        if node.id.isupper():
            return None
        return {"<%s>" % node.id}
    if isinstance(node, ast.Tuple):
        acc = {""}
        for e in node.elts:
            r = _key_reprs(e)
            if r is None:
                return None
            acc = {(a + "," if a else "") + x for a in acc for x in r}
        return {"(" + a + ")" for a in acc}
    return None


def _spread_comp_keys(node):
    """For `**{k: V for k in {A, B}}` return (elements, value dump) or None."""
    if not isinstance(node, ast.DictComp) or len(node.generators) != 1:
        return None
    g = node.generators[0]
    if g.ifs or not isinstance(g.target, ast.Name):
        return None
    if not (isinstance(node.key, ast.Name) and node.key.id == g.target.id):
        return None
    if not isinstance(g.iter, (ast.Set, ast.List, ast.Tuple)):
        return None
    return g.iter.elts, _norm(node.value)


def _dict_entries(node):
    """[(key node or None, value node)] with spreads of literal comprehensions expanded."""
    out = []
    for k, v in zip(node.keys, node.values):
        if k is None:
            sc = _spread_comp_keys(v)
            if sc is not None:
                elts, vd = sc
                for e in elts:
                    out.append((e, ("comp", vd)))
                continue
        out.append((k, v))
    return out


def _vdump(v):
    if isinstance(v, tuple):
        return "comp:" + v[1]
    return _norm(v)


def diff_value(o, n):
    """ALL, or a list of (key repr set, 'val' | 'key') for a changed attribute value."""
    if _norm(o) == _norm(n):
        return []
    if isinstance(o, ast.Dict) and isinstance(n, ast.Dict):
        oe = _dict_entries(o)
        ne = _dict_entries(n)
        if [_norm(v) for k, v in oe if k is None] != [_norm(v) for k, v in ne if k is None]:
            return ALL
        od = {}
        for k, v in oe:
            if k is not None:
                od.setdefault(_norm(k), []).append((k, _vdump(v)))
        nd = {}
        for k, v in ne:
            if k is not None:
                nd.setdefault(_norm(k), []).append((k, _vdump(v)))
        out = []
        for kd in set(od) | set(nd):
            a = od.get(kd)
            b = nd.get(kd)
            knode = (a or b)[0][0]
            reprs = _key_reprs(knode)
            if reprs is None:
                return ALL
            if a is None or b is None or len(a) != len(b):
                out.append((reprs, "key"))
            elif [x[1] for x in a] != [x[1] for x in b]:
                out.append((reprs, "val"))
        return out
    if isinstance(o, ast.Set) and isinstance(n, ast.Set):
        return _elts_diff(o.elts, n.elts)
    if isinstance(o, ast.BinOp) and isinstance(n, ast.BinOp) and type(o.op) is type(n.op):
        if _norm(o.left) == _norm(n.left) and isinstance(o.right, (ast.Set, ast.Dict)):
            r = diff_value(o.right, n.right)
            return r
        if _norm(o.right) == _norm(n.right) and isinstance(o.left, (ast.Set, ast.Dict)):
            return diff_value(o.left, n.left)
        return ALL
    if isinstance(o, ast.Call) and isinstance(n, ast.Call) and _norm(o.func) == _norm(n.func):
        # set(...)/frozenset(...)/dict(...) of a literal
        fn = o.func.id if isinstance(o.func, ast.Name) else None
        if fn in ("set", "frozenset") and len(o.args) == 1 == len(n.args) and not o.keywords and not n.keywords:
            if isinstance(o.args[0], (ast.Set, ast.List, ast.Tuple)) and type(o.args[0]) is type(n.args[0]):
                return _elts_diff(o.args[0].elts, n.args[0].elts)
    return ALL


def _elts_diff(oe, ne):
    os_ = {}
    for e in oe:
        os_.setdefault(_norm(e), e)
    ns_ = {}
    for e in ne:
        ns_.setdefault(_norm(e), e)
    out = []
    for kd in set(os_) ^ set(ns_):
        node = os_.get(kd) or ns_.get(kd)
        if isinstance(node, ast.Starred):
            return ALL
        r = _key_reprs(node)
        if r is None:
            return ALL
        out.append((r, "key"))
    return out


# ---------- change extraction ----------


class Unbounded(Exception):
    """The change is not bounded by the recorded reads; it stays inside its module's dependents."""


class Escapes(Exception):
    """A module-level statement whose effect lands on objects of other modules."""


def _changes_for(mod, scope, key, o, n, out, local):
    """Append semantic changes between old stmt o and new stmt n (either may be None).
    local: the names the scope itself assigns."""
    if o is not None and n is not None and _norm(o) == _norm(n):
        return
    st = o if o is not None else n
    kind = key[0]
    if kind == "doc":
        return
    if kind == "a":
        if isinstance(st, ast.AnnAssign) and (o is None or o.value is None) and (n is None or n.value is None):
            return
        names = key[1]
        if o is None or n is None:
            for nm in names:
                if nm.startswith((".", "?")):
                    raise Escapes("attribute/complex target added/removed")
                if nm.startswith("[]") and nm[2:] not in local:
                    raise Escapes("item of a name the scope does not assign, added/removed")
            if not scope:
                # `from m import name` elsewhere fails at import, which no read records
                raise Unbounded("module global added/removed")
            for nm in names:
                nm = nm[2:] if nm.startswith("[]") else nm
                out.append(("attr", mod, scope, nm, ALL, "added" if o is None else "removed"))
            return
        if len(names) == 1 and not names[0].startswith(("[", ".", "?")):
            ov = o.value
            nv = n.value
            if isinstance(o, ast.AugAssign) or isinstance(n, ast.AugAssign):
                chg = diff_value(ov, nv) if type(o) is type(n) else ALL
            elif type(o) is not type(n) or len(getattr(o, "targets", [])) != len(getattr(n, "targets", [])):
                chg = ALL
            else:
                chg = diff_value(ov, nv)
            if chg == []:
                return
            out.append(("attr", mod, scope, names[0], chg, "changed"))
            return
        for nm in names:
            if nm.startswith("[]"):
                if nm[2:] not in local:
                    raise Escapes("item of a name the scope does not assign")
                out.append(("attr", mod, scope, nm[2:], _subscript_chg(o, n), "changed"))
            elif nm.startswith((".", "?")):
                raise Escapes("attribute/complex target")
            else:
                out.append(("attr", mod, scope, nm, ALL, "changed"))
        return
    if kind == "f":
        if o is None or n is None:
            out.append(("defset", mod, scope, st.name))
            return
        out.append(("func", mod, scope, o.name, _start(o)))
        if [_norm(d) for d in o.decorator_list] != [_norm(d) for d in n.decorator_list]:
            if scope:
                out.append(("class", mod, scope))
            elif n.decorator_list or "TW" not in _map():
                # a decorator call that is new, or gets a new argument, can raise at import
                # (a map without TW does not know who names a decorated def)
                raise Unbounded("module function decorator")
            else:
                # the name is now bound to the plain function: whoever names it sees that
                out.append(("attr", mod, scope, o.name, ALL, "changed"))
        return
    if kind == "c":
        q = st.name if not scope else scope + "." + st.name
        if o is None or n is None:
            raise Unbounded("class added/removed")
        out.append(("class", mod, q))
        return
    if kind == "call":
        tgt = key[1]
        if tgt == "?":
            raise Escapes("module/class-level call")
        name, meth = tgt.split(".", 1)
        if name not in local:
            raise Escapes("method call on a name the scope does not assign")
        chg = ALL
        if o is not None and n is not None and meth in ("pop", "add", "discard", "remove"):
            ks = set()
            for call in (o.value, n.value):
                if call.args:
                    r = _key_reprs(call.args[0])
                    if r is None:
                        ks = None
                        break
                    ks |= r
            if ks is not None:
                chg = [(ks, "key")]
        out.append(("attr", mod, scope, name, chg, "mutated"))
        return
    if kind == "h":
        raise Unbounded("compound statement header")
    raise Unbounded("statement %s" % kind)


def _subscript_chg(o, n):
    ks = set()
    for st in (o, n):
        for t in st.targets:
            if isinstance(t, ast.Subscript):
                r = _key_reprs(t.slice)
                if r is None:
                    return ALL
                ks |= r
    return [(ks, "key")]


def changes(path, old_src, new_src, changed_old_lines, changed_new_lines):
    """Semantic class/module-level changes, or raise Unbounded."""
    mod = module_of(path)
    try:
        ot = ast.parse(old_src)
        nt = ast.parse(new_src)
    except SyntaxError:
        raise Unbounded("syntax")
    osc = Scopes(ot)
    nsc = Scopes(nt)
    keys = []
    for sc, lines in ((osc, changed_old_lines), (nsc, changed_new_lines)):
        for ln in lines:
            loc = sc.locate(ln)
            if loc is None:
                continue
            qual, k, st, in_body = loc
            if in_body:
                continue
            if (qual, k) not in keys:
                keys.append((qual, k))
    out = []
    for qual, k in keys:
        o = osc.stmts.get(qual, {}).get(k)
        n = nsc.stmts.get(qual, {}).get(k)
        if (qual in osc.stmts) != (qual in nsc.stmts):
            raise Unbounded("scope added/removed")
        local = set()
        for sc in (osc, nsc):
            for sk, sst in sc.stmts.get(qual, {}).items():
                # `X = Other.X` or `X = name` aliases an object the scope did not make: not local
                if sk[0] == "a" and not isinstance(getattr(sst, "value", None), (ast.Name, ast.Attribute)):
                    local.update(nm for nm in sk[1] if not nm.startswith(("[", ".", "?")))
        _changes_for(mod, qual, k, o, n, out, local)
    return out


# ---------- map queries ----------


def _set(i):
    return _map()["sets"][i]


def _match(filt, chg):
    if chg == ALL:
        return True
    if filt in ("R", "V"):
        return True
    if filt == "K":
        return any(kind == "key" for _, kind in chg)
    t, _, key = filt.partition(":")
    for reprs, kind in chg:
        if key in reprs:
            if t == "v" or kind == "key":
                return True
    return False


def _users(cls, acc):
    m = _map()
    u = m["users"].get(cls)
    if u is not None:
        acc.update(_set(u))
    for d in m["comps_rev"].get(cls, ()):
        u = m["users"].get(d)
        if u is not None:
            acc.update(_set(u))


def _desc_chg(name, chg, acc, seen, depth=0):
    """Tests reading desc `name` in a way that sees chg, following derivation edges."""
    sk = (name, repr(chg))
    if sk in seen:
        return
    seen.add(sk)
    m = _map()
    d = m["descs"].get(name)
    if d is None:
        return
    vt, reads, edges = d
    for filt, sid in reads.items():
        if _match(filt, chg):
            acc.update(_set(sid))
    if vt.startswith("alias:"):
        src = vt[6:]
        _desc_chg(src, ALL, acc, seen, depth + 1)
    for filt, dtype, dst, kp in edges:
        if not _match(filt, chg):
            continue
        if dtype == "attr":
            _desc_chg(dst, chg if kp else ALL, acc, seen, depth + 1)
            _missing_desc(dst, acc)
        elif dtype == "users":
            for c in dst:
                _users(c, acc)
        elif dtype == "func":
            s = m["F"].get(dst)
            if s is not None:
                acc.update(_set(s))
        elif dtype == "global":
            _global(dst, chg if kp else ALL, acc, seen)
        elif dtype == "all":
            raise Unbounded("derivation into unknown target")


def _missing_desc(name, acc):
    # a derived target with no recorded descriptor (not instrumented): its class's users
    m = _map()
    if name in m["descs"]:
        return
    mod, _, rest = name.partition(":")
    cls = rest.rpartition(".")[0]
    if not cls:
        raise Unbounded("derived module global not recorded")
    _users(mod + ":" + cls, acc)


def _global(name, chg, acc, seen):
    m = _map()
    _desc_chg(name, chg, acc, seen)
    d = m["descs"].get(name)
    if d is None or d[0] != "D":
        # read by name: every test running code that names it, or whose test file does
        g = m["G"].get(name)
        if g is not None:
            acc.update(_set(g))
        w = m.get("TW", {}).get(name.rpartition(":")[2])
        if w is not None:
            acc.update(_set(w))
    for e in m["gedges"].get(name, ()):
        filt, dtype, dst, kp = e
        if dtype == "attr":
            _desc_chg(dst, ALL, acc, seen)
            _missing_desc(dst, acc)
        elif dtype == "global":
            _global(dst, ALL, acc, seen)
        elif dtype == "func":
            s = m["F"].get(dst)
            if s is not None:
                acc.update(_set(s))
        elif dtype == "users":
            for c in dst:
                _users(c, acc)


def _attr_change(mod, scope, name, chg, how, acc, seen):
    m = _map()
    if not scope:
        _global(mod + ":" + name, chg, acc, seen)
        return
    cls = mod + ":" + scope
    if cls not in m["mro"]:
        raise Unbounded("class not recorded")
    dname = cls + "." + name
    d = m["descs"].get(dname)
    if d is None or how in ("added", "removed"):
        # lookups of this name resolved to a base class attribute (or failed)
        found = False
        for b in m["mro"][cls]:
            bn = b + "." + name
            if bn in m["descs"]:
                _desc_chg(bn, ALL, acc, seen)
                found = True
                break
        _users(cls, acc)
        if d is None and not found and how == "changed":
            # value not instrumented (a type, function or descriptor)
            pass
    if d is not None:
        _desc_chg(dname, chg, acc, seen)
        vt = d[0]
        if name in m["post_names"] and vt != "D":
            # read by a metaclass/__init_subclass__ of this class itself, invisibly
            _users(cls, acc)
    elif name in m["post_names"]:
        _users(cls, acc)


def affected(path, old_src, new_src, changed_old_lines, changed_new_lines):
    if not path.endswith(".py") or "sqlglot/" not in "/" + path or path.startswith("tests/"):
        return None
    try:
        chs = changes(path, old_src, new_src, changed_old_lines, changed_new_lines)
        acc = set()
        seen = set()
        m = _map()
        for ch in chs:
            kind = ch[0]
            if kind == "attr":
                _, mod, scope, name, chg, how = ch
                _attr_change(mod, scope, name, chg, how, acc, seen)
            elif kind == "func":
                _, mod, scope, name, line = ch
                fk = "%s:%s:%d" % (path_of(mod, path), (scope + "." if scope else "") + name, line)
                s = m["F"].get(fk)
                if s is not None:
                    acc.update(_set(s))
            elif kind == "defset":
                _, mod, scope, name = ch
                if not scope:
                    raise Unbounded("module function added/removed")
                cls = mod + ":" + scope
                if cls not in m["mro"]:
                    raise Unbounded("class not recorded")
                _users(cls, acc)
            elif kind == "class":
                _, mod, q = ch
                cls = mod + ":" + q
                if cls not in m["mro"]:
                    raise Unbounded("class not recorded")
                _users(cls, acc)
        return acc
    except Unbounded:
        return None
    except Escapes:
        return ALL


def module_users(path):
    """Tests that used a class defined in module `path` (read an attribute or ran a method)."""
    m = _map()
    pre = module_of(path) + ":"
    acc = set()
    for cls, sid in m["users"].items():
        if cls.startswith(pre):
            acc.update(_set(sid))
    return acc


def path_of(mod, path):
    i = path.find("sqlglot/")
    return path[i:] if i >= 0 else path


def why(path, old_src, new_src, changed_old_lines, changed_new_lines):
    """Debug: the extracted changes (or the Unbounded reason)."""
    try:
        return changes(path, old_src, new_src, changed_old_lines, changed_new_lines)
    except (Unbounded, Escapes) as e:
        return "%s: %s" % (type(e).__name__, e)
