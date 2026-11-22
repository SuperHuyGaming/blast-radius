"""Where a source line sits: which function, which statement, or import-time code.

Only the statements a patch touches are looked at.  `Focus` takes a parsed
module and the line numbers of a change (plus the lines right around it) and
keeps the smallest enclosing statements: module-level statements, or members of
classes.  Two versions of a file are the same program when their focused
statements print the same, sit at the same place in the same bodies, and those
bodies have the same length; everything outside the focus is untouched text.
"""

import ast

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


class Scope:
    __slots__ = ("kind", "start", "end", "body_start", "qual", "node", "parent")

    def __init__(self, kind, start, end, body_start, qual, node, parent):
        self.kind = kind  # "def" or "lambda"
        self.start = start  # first line, decorators included
        self.end = end
        self.body_start = body_start
        self.qual = qual
        self.node = node
        self.parent = parent  # enclosing Scope or None

    def owns_body(self, line):
        """True when `line` only runs when the function is called."""
        if self.kind == "def":
            return self.body_start > self.node.lineno and self.body_start <= line <= self.end
        return self.node.lineno < line < self.end

    def named(self):
        s = self
        while s is not None and s.kind != "def":
            s = s.parent
        return s


def _span(node):
    if isinstance(node, ast.match_case):
        return node.pattern.lineno, node.body[-1].end_lineno
    start = node.lineno
    for d in getattr(node, "decorator_list", ()):
        start = min(start, d.lineno)
    return start, node.end_lineno


class Focus:
    def __init__(self, tree, lines):
        self.tree = tree
        self.items = []  # (class path, index in body, body length, statement, first in its body)
        self.scopes = []
        self.by_qual = {}
        self.names = []  # qualified names of every def and class in focus
        self.strings = []  # (start, end) of string literals spanning lines
        self._pick(tree.body, "", sorted(set(lines)))
        for prefix, _, _, node, _ in self.items:
            self._visit(node, prefix, None)
        self.scopes.sort(key=lambda s: (s.end - s.start, s.start))
        self._keys = None
        self._keys_nodoc = None

    # -- choosing the statements
    def _pick(self, body, prefix, lines):
        n = len(body)
        for i, st in enumerate(body):
            a, b = _span(st)
            inside = [ln for ln in lines if a <= ln <= b]
            if not inside:
                continue
            if isinstance(st, ast.ClassDef) and st.body:
                first = _span(st.body[0])[0]
                if all(ln >= first for ln in inside):
                    # nothing on the class line itself: look at its members instead
                    self._pick(st.body, prefix + st.name + ".", inside)
                    continue
            self.items.append((prefix, i, n, st, i == 0))

    # -- comparing two versions
    def keys(self):
        if self._keys is None:
            self._keys = [(p, i, n, ast.dump(st)) for p, i, n, st, _ in self.items]
        return self._keys

    def keys_nodoc(self):
        """Like keys(), with every docstring blanked."""
        if self._keys_nodoc is None:
            saved = []
            for p, i, n, st, first in self.items:
                if first:
                    _blank(st, saved)
                for node in ast.walk(st):
                    if isinstance(node, DOC_OWNERS) and node.body:
                        _blank(node.body[0], saved)
            self._keys_nodoc = [(p, i, n, ast.dump(st)) for p, i, n, st, _ in self.items]
            for const, value in saved:  # the tree itself stays as parsed
                const.value = value
        return self._keys_nodoc

    def keys_noann(self):
        """Like keys(), with parameter and return annotations of functions removed."""
        saved = []
        for st in self.nodes():
            for node in ast.walk(st):
                if isinstance(node, FUNC):
                    if node.returns is not None:
                        saved.append((node, "returns", node.returns))
                        node.returns = None
                    a = node.args
                    for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]:
                        if arg.annotation is not None:
                            saved.append((arg, "annotation", arg.annotation))
                            arg.annotation = None
        out = [(p, i, n, ast.dump(st)) for p, i, n, st, _ in self.items]
        for node, field, value in saved:  # the tree itself stays as parsed
            setattr(node, field, value)
        return out

    def places(self):
        return [(p, i, n) for p, i, n, _, _ in self.items]

    def nodes(self):
        return [st for _, _, _, st, _ in self.items]

    # -- scopes inside the focus
    def _visit(self, child, prefix, parent):
        if isinstance(child, FUNC):
            qual = prefix + child.name
            start = min([d.lineno for d in child.decorator_list] + [child.lineno])
            sc = Scope("def", start, child.end_lineno, child.body[0].lineno, qual, child, parent)
            self.scopes.append(sc)
            self.by_qual.setdefault(qual, sc)
            self.names.append("def " + qual)
            self._walk(child, qual + ".", sc)
        elif isinstance(child, ast.Lambda):
            sc = Scope("lambda", child.lineno, child.end_lineno, child.body.lineno, prefix + "<lambda>", child, parent)
            self.scopes.append(sc)
            self._walk(child, prefix, sc)
        elif isinstance(child, ast.ClassDef):
            qual = prefix + child.name
            self.names.append("class " + qual)
            self._walk(child, qual + ".", parent)
        else:
            if isinstance(child, (ast.Constant, ast.JoinedStr)) and child.end_lineno > child.lineno:
                if not isinstance(child, ast.Constant) or isinstance(child.value, (str, bytes)):
                    self.strings.append((child.lineno, child.end_lineno))
            self._walk(child, prefix, parent)

    def _walk(self, node, prefix, parent):
        for child in ast.iter_child_nodes(node):
            self._visit(child, prefix, parent)

    def scope(self, line):
        for sc in self.scopes:  # smallest span first
            if sc.start <= line <= sc.end:
                return sc
        return None

    def in_string(self, line):
        for a, b in self.strings:
            if a <= line <= b and (a < line or b > line):
                return True
        return False

    def statement_lines(self, sc, line):
        """Lines of the innermost statement of `sc` holding `line` (only its
        header when it has a body), or None when that cannot be pinned down."""
        try:
            return _find(sc.node.body, line)
        except Exception:
            return None


def _blank(stmt, saved):
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    ):
        saved.append((stmt.value, stmt.value.value))
        stmt.value.value = ""


def _find(stmts, line):
    for s in stmts:
        a, b = _span(s)
        if not (a <= line <= b):
            continue
        children = []
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            v = getattr(s, field, None)
            if isinstance(v, list):
                children.extend(v)
        if not children:
            return range(a, b + 1)
        inner = _find(children, line)
        if inner is not None:
            return inner
        first = min(_span(c)[0] for c in children)
        if line < first:
            return range(a, first)
        return None
    return None
