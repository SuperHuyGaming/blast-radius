"""Binding-aware sharing groups of the droppable statements of a test method.

Two droppable statements share an object when both read the same binding of a local name
(the same assignment reaches both reads: no statement between them binds the name again).
'self' is not counted (every validate_* statement reads it). A droppable statement binds no
name that a later statement reads, so the bindings that reach two droppable statements come
from statements that always run (or from the method's arguments). Components of the relation
"shares an object" (transitive closure) are returned for the droppable statements."""
from __future__ import annotations

import ast


def _names(node, ctx):
    return {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ctx)}


def _binds(node):
    out = _names(node, ast.Store) | _names(node, ast.Del)
    for x in ast.walk(node):
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(x.name)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            for a in x.names:
                out.add(a.asname or a.name.partition(".")[0])
        elif isinstance(x, ast.ExceptHandler) and x.name:
            out.add(x.name)
        elif isinstance(x, (ast.MatchAs, ast.MatchStar)) and x.name:
            out.add(x.name)
        elif isinstance(x, ast.NamedExpr):
            out.add(x.target.id)
    return out


def method_node(file_lines, first, last):
    fn = ast.parse("if 1:\n" + "\n".join(file_lines[first - 1:last]) + "\n").body[0].body[0]
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise ValueError("not a function")
    return fn


def share_components(file_lines, shape):
    """{statement index: frozenset of droppable statement indices} for every droppable
    statement (its component, itself included). Raises on a method it cannot read."""
    first, last, stmts = shape
    fn = method_node(file_lines, first, last)
    if len(fn.body) != len(stmts):
        raise ValueError("statement count differs")
    args = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    if fn.args.vararg:
        args.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        args.add(fn.args.kwarg.arg)
    binds = [_binds(s) for s in fn.body]
    local = set(args)
    for b in binds:
        local |= b
    local.discard("self")
    reads = [(_names(s, ast.Load) | _names(s, ast.Del)) & local for s in fn.body]
    droppable = [i for i, st in enumerate(stmts) if st[2]]
    keys = {}
    for j in droppable:
        ks = set()
        for x in reads[j]:
            prev = [b for b in range(j) if x in binds[b]]
            if prev:
                ks.add((x, prev[-1]))
            elif x in args:
                ks.add((x, -1))
            elif x in binds[j]:
                continue  # only the statement's own binding can reach the read
            else:
                ks.add((x, -2))  # read before any binding (a later binding of an earlier loop...)
        keys[j] = ks
    # union-find over shared keys
    parent = {j: j for j in droppable}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    owner = {}
    for j in droppable:
        for k in keys[j]:
            o = owner.get(k)
            if o is None:
                owner[k] = j
            else:
                ra, rb = find(o), find(j)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    comp = {}
    for j in droppable:
        comp.setdefault(find(j), set()).add(j)
    return {j: frozenset(comp[find(j)]) for j in droppable}


def forced_statements(file_lines, shape):
    """The droppable statements that a statement which always runs depends on: it reads a
    binding of a local name that such a statement also reads after the binding (and so can
    have changed: expr.set(...), then a with block that checks expr.sql()). Closed under the
    same rule (a forced statement always runs, so what it reads counts too)."""
    first, last, stmts = shape
    fn = method_node(file_lines, first, last)
    if len(fn.body) != len(stmts):
        raise ValueError("statement count differs")
    binds = [_binds(s) for s in fn.body]
    local = set(a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs)
    for b in binds:
        local |= b
    local.discard("self")
    reads = [(_names(s, ast.Load) | _names(s, ast.Del)) & local for s in fn.body]
    always = {i for i, st in enumerate(stmts) if not st[2]}
    forced = set()
    changed = True
    while changed:
        changed = False
        for b in sorted(always | forced):
            for x in reads[b]:
                prev = [d for d in range(b) if x in binds[d]]
                start = prev[-1] if prev else -1
                for j in range(start + 1, b):
                    if stmts[j][2] and j not in forced and x in reads[j]:
                        forced.add(j)
                        changed = True
    return forced


ALLOWED = (ast.Expr, ast.With, ast.For, ast.If, ast.Try, ast.While, ast.Assign, ast.AnnAssign)


def _escapes(node):
    for x in ast.walk(node):
        if isinstance(x, (ast.Return, ast.Yield, ast.YieldFrom, ast.Await, ast.Global, ast.Nonlocal)):
            return True
    return False


def _stores_state(node):
    for x in ast.walk(node):
        if isinstance(x, (ast.Attribute, ast.Subscript)) and isinstance(x.ctx, (ast.Store, ast.Del)):
            return True
    return False


def _definite(node):
    """Names that a top-level statement binds whenever it ends normally: plain assignments."""
    out = set()
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)) and all(isinstance(e, ast.Name) for e in t.elts):
                out.update(e.id for e in t.elts)
    elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
        out.add(node.target.id)
    return out


def analyze(file_lines, shape):
    """Statement groups of a test method, binding-aware.

    Candidates: statements that reduce.method_shape would call droppable but for the rule that
    no later statement may read a name they bind (no return / yield / global, no attribute or
    item store, no augmented assignment, no docstring). Two statements are joined when one
    binds a name and the other reads that binding (every binding since the last plain
    assignment of the name before the read counts), and when both read the same binding.
    A component of candidates only (no other statement joined to it) can be left out as a
    whole: all its statements become droppable and form one group. In a component with a
    statement that always runs, the statements keep their flags, and a droppable statement
    that shares a binding with such a statement always runs too (forced).

    Returns (droppable flags, {droppable statement: frozenset group}, forced, freed)."""
    first, last, stmts = shape
    fn = method_node(file_lines, first, last)
    body = fn.body
    if len(body) != len(stmts):
        raise ValueError("statement count differs")
    n = len(body)
    args = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    if fn.args.vararg:
        args.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        args.add(fn.args.kwarg.arg)
    binds = [_binds(s) for s in body]
    defin = [_definite(s) for s in body]
    local = set(args)
    for b in binds:
        local |= b
    local.discard("self")
    reads = [(_names(s, ast.Load) | _names(s, ast.Del)) & local for s in body]
    cand = []
    for i, s in enumerate(body):
        ok = isinstance(s, ALLOWED)
        if ok and isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
            ok = False
        if ok and (_escapes(s) or _stores_state(s)):
            ok = False
        cand.append(ok)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def join(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    owner = {}
    for j in range(n):
        for x in reads[j]:
            reach = []
            for b in range(j - 1, -1, -1):
                if x in binds[b]:
                    reach.append(b)
                    if x in defin[b]:
                        break
            keys = [(x, b) for b in reach]
            if not reach:
                if x in args:
                    keys = [(x, -1)]
                elif x in binds[j]:
                    continue
                else:
                    keys = [(x, -2)]
            for b in reach:
                join(b, j)
            for k in keys:
                o = owner.get(k)
                if o is None:
                    owner[k] = j
                else:
                    join(o, j)
    comps = {}
    for i in range(n):
        comps.setdefault(find(i), set()).add(i)
    flags = [bool(st[2]) for st in stmts]
    groups = {}
    freed = set()
    forced = set()
    for root, members in comps.items():
        if all(cand[i] for i in members):
            if any(not flags[i] for i in members):
                freed |= {i for i in members if not flags[i]}
            for i in members:
                flags[i] = True
            g = frozenset(members)
            for i in members:
                groups[i] = g
        else:
            # pinned component: droppable statements that share a binding with a statement that
            # always runs run always too; the others group by sharing as before
            pinned = {i for i in members if not flags[i]}
            for j in sorted(members - pinned):
                pass
            forced_here = set()
            for b in sorted(pinned):
                for x in reads[b]:
                    lastdef = -1
                    for d in range(b - 1, -1, -1):
                        if x in defin[d]:
                            lastdef = d
                            break
                    for j in range(lastdef + 1, b):
                        if flags[j] and j in members and x in reads[j]:
                            forced_here.add(j)
            forced |= forced_here
            for j in forced_here:
                flags[j] = False
    # groups of the droppable statements of pinned components: sharing components among them
    rest = [i for i in range(n) if flags[i] and i not in groups]
    if rest:
        sub = share_components(file_lines, (first, last, [(s, e, flags[i] and i in set(rest)) for i, (s, e, _) in enumerate(stmts)]))
        for i in rest:
            groups[i] = sub.get(i, frozenset((i,)))
    return flags, groups, forced, freed
