"""Records, per comparison site in sqlglot, whether `is` and `==` (or `is not` and `!=`) ever disagree.

Loaded by every python process the recording run starts, spawned pool workers included
(tools/eqv is on PYTHONPATH). With EQV_OUT=<dir> and EQV_ROOT=<sqlglot tree>, modules under
<root>/sqlglot are compiled from an AST where each single-operator `a OP b` with OP in
is / is not / == / != becomes `__eqv_OP__(key, a, b)`. The helper returns exactly `a OP b`
(operands evaluated once, in order, original exceptions re-raised) and also evaluates the
swapped operator. Each process appends to <dir>/<pid>.txt as it goes, so a killed worker
keeps what it saw:
  R <key>  the site ran
  D <key>  the swap gave a different result, or a non-bool where the site gave a bool
  E <key>  the swap raised, or the original raised where the swap would not
key = <path from root>:<line>:<col>:<op>. Chained comparisons are left alone (no key).
"""

import os

_out = os.environ.get("EQV_OUT")
_root = os.environ.get("EQV_ROOT")
if _out and _root:
    import ast
    import builtins
    import sys
    import threading
    from importlib.machinery import PathFinder, SourceFileLoader

    _root = os.path.realpath(_root)
    _pkg = os.path.join(_root, "sqlglot") + os.sep
    _OPS = {ast.Is: "is", ast.IsNot: "isnot", ast.Eq: "eq", ast.NotEq: "ne"}
    _seen = set()
    _fd = [None, None]  # fd, pid it belongs to

    class _State(threading.local):
        busy = False  # inside a swapped evaluation: those calls never happen in a real run

    _st = _State()

    def _emit(tag, key):
        line = tag + " " + key
        if line in _seen:
            return
        _seen.add(line)
        pid = os.getpid()
        if _fd[1] != pid:
            _fd[0] = os.open(os.path.join(_out, "%d.txt" % pid), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            _fd[1] = pid
        os.write(_fd[0], (line + "\n").encode())

    def __eqv_is__(key, a, b):
        r = a is b
        if _st.busy:
            return r
        _emit("R", key)
        _st.busy = True
        try:
            s = a == b
            if type(s) is not bool or s is not r:
                _emit("D", key)
        except BaseException:
            _emit("E", key)
        finally:
            _st.busy = False
        return r

    def __eqv_isnot__(key, a, b):
        r = a is not b
        if _st.busy:
            return r
        _emit("R", key)
        _st.busy = True
        try:
            s = a != b
            if type(s) is not bool or s is not r:
                _emit("D", key)
        except BaseException:
            _emit("E", key)
        finally:
            _st.busy = False
        return r

    def __eqv_eq__(key, a, b):
        if _st.busy:
            return a == b
        _emit("R", key)
        try:
            r = a == b
        except BaseException:
            _emit("E", key)
            raise
        if type(r) is not bool or r is not (a is b):
            _emit("D", key)
        return r

    def __eqv_ne__(key, a, b):
        if _st.busy:
            return a != b
        _emit("R", key)
        try:
            r = a != b
        except BaseException:
            _emit("E", key)
            raise
        if type(r) is not bool or r is not (a is not b):
            _emit("D", key)
        return r

    for _f in (__eqv_is__, __eqv_isnot__, __eqv_eq__, __eqv_ne__):
        setattr(builtins, _f.__name__, _f)

    class _Rewrite(ast.NodeTransformer):
        def __init__(self, rel):
            self.rel = rel

        def visit_Compare(self, node):
            self.generic_visit(node)
            op = _OPS.get(type(node.ops[0]))
            if len(node.ops) != 1 or op is None:
                return node
            key = "%s:%d:%d:%s" % (self.rel, node.lineno, node.col_offset, op)
            call = ast.Call(
                func=ast.Name(id="__eqv_%s__" % op, ctx=ast.Load()),
                args=[ast.Constant(key), node.left, node.comparators[0]],
                keywords=[],
            )
            return ast.copy_location(call, node)

        # annotations stay as written (`from __future__ import annotations` keeps their text)
        def visit_arg(self, node):
            return node

        def visit_AnnAssign(self, node):
            if node.value is not None:
                node.value = self.visit(node.value)
            return node

        def _visit_def(self, node):
            returns = node.returns
            node.returns = None
            self.generic_visit(node)
            node.returns = returns
            return node

        visit_FunctionDef = _visit_def
        visit_AsyncFunctionDef = _visit_def

    class _Loader(SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(self.get_data(self.path), self.path)
            rel = os.path.relpath(self.path, _root).replace(os.sep, "/")
            tree = ast.fix_missing_locations(_Rewrite(rel).visit(tree))
            return compile(tree, self.path, "exec", dont_inherit=True)

    class _Finder:
        @staticmethod
        def find_spec(name, path=None, target=None):
            if name != "sqlglot" and not name.startswith("sqlglot."):
                return None
            spec = PathFinder.find_spec(name, path, target)
            if spec is not None and type(spec.loader) is SourceFileLoader and spec.origin.startswith(_pkg):
                spec.loader = _Loader(spec.loader.name, spec.loader.path)
            return spec

        @staticmethod
        def invalidate_caches():
            pass

    sys.meta_path.insert(0, _Finder)
