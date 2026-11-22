"""Records, per operator site in sqlglot and per test, whether a one-operator mutant of the
site would give a different result at any evaluation.

Loaded by every python process the recording run starts (tools/prof is on PYTHONPATH), with
profplug.py as the pytest plugin of the main process. With PROF_OUT=<dir> and PROF_ROOT=<sqlglot
tree>, modules under <root>/sqlglot are compiled from a rewritten AST:
- a one-operator comparison with is, is not, ==, !=, <, <=, >, >= becomes a helper call that
  returns exactly `a OP b` and also evaluates its partner (is/==, is not/!=, </<=, >/>=);
- a BoolOp `v1 op ... op vn` becomes a helper call on one thunk per operand. It evaluates them
  as Python does and, for each of the n-1 operator tokens, works out what the source with that
  token flipped (and <-> or, under Python's precedence) would give: an operand the mutant needs
  that the original skipped is evaluated only when it is pure (names, constants, attribute and
  subscript loads, comparisons, not, isinstance/len), otherwise the token differs; an operand
  the original ran and the mutant skips must be pure too. In a truth-tested place (if, while,
  assert, ternary test, comprehension filter, under not, inside such a BoolOp) only the truth
  of the result counts, elsewhere the mutant must give the very same object.
  BoolOps in class bodies, or holding yield, await, := or super, are left alone (no key).
Each process appends to <dir>/<pid>.txt:
  R <key>...       the site ran (a BoolOp: one key per token)
  X <key>          the mutant differs at an evaluation outside any test (collection, an
                   import, a pool worker between tasks): the site is not profiled
  T <ctx>\\t<keys>  the test (or scope) ctx finished; keys are the sites that differed in it
  C <ctx>          the test ran its call phase and was not skipped
key = <path from root>:<line>:<col>:<op>, op one of is isnot eq ne lt le gt ge, or and<k> / or<k>
for the k-th token (from 1) of a BoolOp. ctx is a pytest node id, or scope:<node id> while a
class/module/session fixture sets up.
"""

import os

_out = os.environ.get("PROF_OUT")
_root = os.environ.get("PROF_ROOT")
if _out and _root:
    import ast
    import builtins
    import sys
    import threading
    from importlib.machinery import PathFinder, SourceFileLoader

    _root = os.path.realpath(_root)
    _pkg = os.path.join(_root, "sqlglot") + os.sep
    _fd = [None, None]  # fd, pid it belongs to
    _pool_child = "--multiprocessing-fork" in getattr(sys, "orig_argv", ())
    # a plain child (subprocess) belongs to the test that started it; a pool worker only to
    # the task it is running
    _ctx = [None]
    _imports = [0]  # depth of sqlglot module bodies running
    _diff: dict = {}  # ctx -> set of keys that differed

    class _State(threading.local):
        busy = False  # inside a mutant's evaluation: those calls never happen in a real run
        # the site a mutant's evaluation is for, and whether that evaluation reached the site
        # again: there the mutant would run too (`cls == other` inside __eq__ recurses), which
        # the original semantics used inside a mutant's evaluation cannot tell
        probe = None
        again = False

    _st = _State()

    def _write(line):
        pid = os.getpid()
        if _fd[1] != pid:
            _fd[0] = os.open(os.path.join(_out, "%d.txt" % pid), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            _fd[1] = pid
            _ranset.clear()
            _xset.clear()
            _diff.clear()
            _enter(_ctx[0])
        os.write(_fd[0], (line + "\n").encode())

    _ranset = set()

    def _ran(key):
        if key not in _ranset:
            _write("R " + key)
            _ranset.add(key)

    _xset = set()  # sites known unprofiled in this process: nothing more to learn
    _cur = [_xset]  # keys known to differ in the current ctx

    def _known(key):
        # in a module body a difference is an X even when the test already has the key
        return key in _xset or (not _imports[0] and key in _cur[0])

    def _differs(key):
        if _ctx[0] is None or _imports[0]:
            if key not in _xset:
                _write("X " + key)
                _xset.add(key)
            return
        _cur[0].add(key)

    def flush(ctx):
        """Writes what ctx saw; profplug calls it when a test or scope ends."""
        _write("T " + ctx + "\t" + " ".join(sorted(_diff.pop(ctx, ()))))

    def _enter(ctx):
        _ctx[0] = ctx
        if ctx is None:
            _cur[0] = _xset
        else:
            s = _diff.get(ctx)
            if s is None:
                s = _diff[ctx] = set()
            _cur[0] = s

    def set_ctx(ctx):
        _enter(ctx)
        if not _pool_child:
            if ctx is None:
                os.environ.pop("PROF_CTX", None)
            else:
                os.environ["PROF_CTX"] = ctx

    def get_ctx():
        return _ctx[0]

    def _same(r, s):
        return type(r) is bool and r is s

    def _cmp(name, op, partner):
        def helper(key, a, b):
            if _st.busy:
                if key == _st.probe:
                    _st.again = True
                return op(a, b)
            _ran(key)
            if _known(key):
                return op(a, b)
            try:
                r = op(a, b)
            except BaseException:
                _differs(key)
                raise
            _st.busy = True
            _st.probe = key
            _st.again = False
            try:
                if not _same(r, partner(a, b)) or _st.again:
                    _differs(key)
            except BaseException:
                _differs(key)
            finally:
                _st.busy = False
                _st.probe = None
            return r

        helper.__name__ = name
        setattr(builtins, name, helper)

    import operator as _o

    _CMP = {
        ast.Is: ("is", _o.is_, _o.eq),
        ast.IsNot: ("isnot", _o.is_not, _o.ne),
        ast.Eq: ("eq", _o.eq, _o.is_),
        ast.NotEq: ("ne", _o.ne, _o.is_not),
        ast.Lt: ("lt", _o.lt, _o.le),
        ast.LtE: ("le", _o.le, _o.lt),
        ast.Gt: ("gt", _o.gt, _o.ge),
        ast.GtE: ("ge", _o.ge, _o.gt),
    }
    for _n, _op, _p in _CMP.values():
        _cmp("__prof_%s__" % _n, _op, _p)

    _UNKNOWN = object()

    def _eval(form, get, used):
        """Python's value of a formula over operand indexes: ("and"|"or", [parts]) or an index.
        `get(i)` gives operand i or _UNKNOWN; `used` collects the operands evaluated."""
        if type(form) is int:
            used.add(form)
            return get(form)
        isand, parts = form
        v = _UNKNOWN
        for p in parts:
            v = _eval(p, get, used)
            if v is _UNKNOWN:
                return v
            if (not v) if isand else v:
                return v
        return v

    def _mutants(isand, n):
        """Formula of the BoolOp with token k flipped, for k = 1..n-1."""
        out = []
        for k in range(1, n):
            if isand:
                # v1 and .. vk OR vk+1 and .. vn
                left = list(range(k))
                right = list(range(k, n))
                out.append((False, [left[0] if len(left) == 1 else (True, left), right[0] if len(right) == 1 else (True, right)]))
            else:
                # v1 or .. or (vk and vk+1) or .. vn
                parts = list(range(k - 1)) + [(True, [k - 1, k])] + list(range(k + 1, n))
                out.append((False, parts))
        return out

    _FORMS: dict = {}

    def __prof_bool__(keys, isand, truth, pure, *thunks):
        if _st.busy:
            if keys[0] == _st.probe:
                _st.again = True
            v = None
            for th in thunks:
                v = th()
                if (not v) if isand else v:
                    return v
            return v
        if keys[0] not in _ranset:
            _write("R " + " ".join(keys[1:]))
            _ranset.add(keys[0])
        if all(_known(k) for k in keys[1:]):
            v = None
            for th in thunks:
                v = th()
                if (not v) if isand else v:
                    return v
            return v
        n = len(thunks)
        vals = []
        try:
            for th in thunks:
                v = th()
                vals.append(v)
                if (not v) if isand else v:
                    break
        except BaseException:
            for k in keys[1:]:
                _differs(k)
            raise
        res = vals[-1]
        ran = len(vals)
        forms = _FORMS.get((isand, n))
        if forms is None:
            forms = _FORMS[(isand, n)] = _mutants(isand, n)
        spec = {}

        def get(i):
            if i < ran:
                return vals[i]
            if i in spec:
                return spec[i]
            if not pure[i]:
                return _UNKNOWN
            try:
                v = thunks[i]()
            except BaseException:
                v = _UNKNOWN
            spec[i] = v
            return v

        _st.busy = True
        _st.probe = keys[0]
        _st.again = False
        try:
            for k, form in enumerate(forms, 1):
                used = set()
                try:
                    m = _eval(form, get, used)
                    if m is _UNKNOWN:
                        same = False
                    elif truth:
                        same = bool(m) is bool(res)
                    else:
                        same = m is res
                    if same:
                        # operands the original ran and the mutant skips must not matter
                        same = all(pure[i] for i in range(ran) if i not in used)
                except BaseException:
                    same = False
                if not same or _st.again:
                    _differs(keys[k])
            if _st.again:
                for k in keys[1:]:
                    _differs(k)
        finally:
            _st.busy = False
            _st.probe = None
        return res

    builtins.__prof_bool__ = __prof_bool__

    _PURE_CALLS = {"isinstance", "len", "bool", "callable"}
    _IMPURE_ATTRS = {"meta"}  # Expr.meta creates its dict on first read

    def _pure(node):
        if isinstance(node, (ast.Name, ast.Constant)):
            return True
        if isinstance(node, ast.Attribute):
            return node.attr not in _IMPURE_ATTRS and _pure(node.value)
        if isinstance(node, ast.Subscript):
            return _pure(node.value) and _pure(node.slice)
        if isinstance(node, ast.Compare):
            return _pure(node.left) and all(_pure(c) for c in node.comparators)
        if isinstance(node, ast.UnaryOp):
            return _pure(node.operand)
        if isinstance(node, ast.BoolOp):
            return all(_pure(v) for v in node.values)
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(_pure(e) for e in node.elts)
        if isinstance(node, ast.Call):
            return (
                isinstance(node.func, ast.Name)
                and node.func.id in _PURE_CALLS
                and not node.keywords
                and all(_pure(a) for a in node.args)
            )
        return False

    def _unsafe(node):
        for n in ast.walk(node):
            if isinstance(n, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)):
                return True
            if isinstance(n, ast.Name) and n.id in ("super", "__class__", "locals", "vars"):
                return True
        return False

    _TRUTH_FIELDS = {
        (ast.If, "test"),
        (ast.While, "test"),
        (ast.IfExp, "test"),
        (ast.Assert, "test"),
        (ast.comprehension, "ifs"),
    }

    class _Rewrite(ast.NodeTransformer):
        def __init__(self, rel):
            self.rel = rel
            self.in_class = False
            self.truth = set()  # ids of BoolOp nodes whose value is only truth-tested

        def visit(self, node):
            for field, value in ast.iter_fields(node):
                items = value if isinstance(value, list) else [value]
                for v in items:
                    if isinstance(v, ast.BoolOp) and (
                        (type(node), field) in _TRUTH_FIELDS
                        or (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not))
                        or (isinstance(node, ast.BoolOp) and id(node) in self.truth)
                    ):
                        self.truth.add(id(v))
            return super().visit(node)

        def visit_Compare(self, node):
            self.generic_visit(node)
            spec = _CMP.get(type(node.ops[0]))
            if len(node.ops) != 1 or spec is None:
                return node
            key = "%s:%d:%d:%s" % (self.rel, node.lineno, node.col_offset, spec[0])
            call = ast.Call(
                func=ast.Name(id="__prof_%s__" % spec[0], ctx=ast.Load()),
                args=[ast.Constant(key), node.left, node.comparators[0]],
                keywords=[],
            )
            return ast.copy_location(call, node)

        def visit_BoolOp(self, node):
            truth = id(node) in self.truth
            pure = tuple(_pure(v) for v in node.values)
            skip = self.in_class or _unsafe(node)
            self.generic_visit(node)
            if skip:
                return node
            isand = isinstance(node.op, ast.And)
            key = "%s:%d:%d:%s" % (self.rel, node.lineno, node.col_offset, "and" if isand else "or")
            keys = (key,) + tuple("%s%d" % (key, k) for k in range(1, len(node.values)))
            thunks = [
                ast.Lambda(
                    args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                    body=v,
                )
                for v in node.values
            ]
            call = ast.Call(
                func=ast.Name(id="__prof_bool__", ctx=ast.Load()),
                args=[ast.Constant(keys), ast.Constant(isand), ast.Constant(truth), ast.Constant(pure), *thunks],
                keywords=[],
            )
            return ast.copy_location(call, node)

        def visit_ClassDef(self, node):
            node.decorator_list = [self.visit(d) for d in node.decorator_list]
            node.bases = [self.visit(b) for b in node.bases]
            node.keywords = [self.visit(k) for k in node.keywords]
            was = self.in_class
            self.in_class = True
            node.body = [self.visit(s) for s in node.body]
            self.in_class = was
            return node

        # annotations stay as written (`from __future__ import annotations` keeps their text)
        def visit_arg(self, node):
            return node

        def visit_AnnAssign(self, node):
            if node.value is not None:
                node.value = self.visit(node.value)
            return node

        def _visit_def(self, node):
            # decorators and defaults run in the enclosing scope, the body in its own
            node.decorator_list = [self.visit(d) for d in node.decorator_list]
            node.args = self.visit(node.args)
            was = self.in_class
            self.in_class = False
            node.body = [self.visit(s) for s in node.body]
            self.in_class = was
            return node

        visit_FunctionDef = _visit_def
        visit_AsyncFunctionDef = _visit_def

        def visit_Lambda(self, node):
            node.args = self.visit(node.args)
            was = self.in_class
            self.in_class = False
            node.body = self.visit(node.body)
            self.in_class = was
            return node

    class _Loader(SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(self.get_data(self.path), self.path)
            rel = os.path.relpath(os.path.realpath(self.path), _root).replace(os.sep, "/")
            tree = ast.fix_missing_locations(_Rewrite(rel).visit(tree))
            return compile(tree, self.path, "exec", dont_inherit=True)

        def exec_module(self, module):
            # a module body runs once a process: what it computes serves every later test
            _imports[0] += 1
            try:
                return super().exec_module(module)
            finally:
                _imports[0] -= 1

    class _Finder:
        @staticmethod
        def find_spec(name, path=None, target=None):
            if name != "sqlglot" and not name.startswith("sqlglot."):
                return None
            spec = PathFinder.find_spec(name, path, target)
            if spec is not None and type(spec.loader) is SourceFileLoader and os.path.realpath(spec.origin).startswith(_pkg):
                spec.loader = _Loader(spec.loader.name, spec.loader.path)
            return spec

        @staticmethod
        def invalidate_caches():
            pass

    sys.meta_path.insert(0, _Finder)

    class Task:
        """A pool task run under the ctx that submitted it (picklable by reference)."""

        def __init__(self, ctx, fn):
            self.ctx = ctx
            self.fn = fn

        def __call__(self, *a, **k):
            # a worker serves many tests: build the generator's dispatch table in each task,
            # as profplug does in each test
            g = sys.modules.get("sqlglot.generator")
            if g is not None and isinstance(getattr(g, "_DISPATCH_CACHE", None), dict):
                g._DISPATCH_CACHE.clear()
            was = _ctx[0]
            _enter(self.ctx)
            try:
                return self.fn(*a, **k)
            finally:
                if self.ctx is not None:
                    flush(self.ctx)
                _enter(was)

    import concurrent.futures.process as _cfp

    _submit = _cfp.ProcessPoolExecutor.submit
    _init = _cfp.ProcessPoolExecutor.__init__

    def _psubmit(self, fn, /, *a, **k):
        return _submit(self, Task(_ctx[0], fn), *a, **k)

    def _pinit(self, *a, initializer=None, **k):
        if initializer is not None:
            initializer = Task(_ctx[0], initializer)
        _init(self, *a, initializer=initializer, **k)

    _cfp.ProcessPoolExecutor.submit = _psubmit
    _cfp.ProcessPoolExecutor.__init__ = _pinit

    def _at_exit():
        for ctx in list(_diff):
            if _diff[ctx]:
                flush(ctx)

    import atexit

    atexit.register(_at_exit)
    _enter(None if _pool_child else os.environ.get("PROF_CTX"))
