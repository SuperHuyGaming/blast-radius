"""Loading sqlglot with a patch applied and finding out what the patch changed.

The selector keeps one process with the pinned sqlglot fully imported.  For a
patch whose effect cannot be read off the source alone (class attributes, table
entries, anything that runs at import time), a forked child re-imports the
patched module and everything that depends on it, then compares the resulting
classes and module globals with the originals.  Each difference is looked up in
the maps recorded at the pinned commit: who reads that attribute, who looks up
that key.
"""

import enum
import functools
import importlib
import importlib.abc
import importlib.util
import os
import re
import sys
import time
import types

from blastkeys import ALL, cname, knorm

SKIP = {
    "__module__", "__qualname__", "__doc__", "__dict__", "__weakref__", "__annotations__",
    "__firstlineno__", "__static_attributes__", "__parameters__", "__orig_bases__",
    "__abstractmethods__", "_abc_impl", "__classcell__", "__type_params__", "__builtins__",
    "__cached__", "__file__", "__loader__", "__spec__", "__name__", "__package__", "__path__",
    "__annotate__", "__annotate_func__", "__annotations_cache__", "__conditional_annotations__",
}
PLAIN = (int, str, bytes, float, bool, type(None), complex)


class Warm:
    """The pinned sqlglot, imported once."""

    def __init__(self, src, maps):
        self.src = src
        self.maps = maps
        if src not in sys.path:
            sys.path.insert(0, src)
        os.environ["SKIP_INTEGRATION"] = "0"
        t0 = time.process_time()
        import unittest  # noqa: sqlglot validates more when it sees unittest loaded, as under pytest

        import sqlglot  # noqa

        files = maps.head["deps"]["files"]
        for name in maps.head["module_order"]:
            if name.startswith("sqlglot") and name in files and name not in sys.modules:
                try:
                    importlib.import_module(name)
                except Exception:
                    pass
        self.files = files  # module name -> relative path
        self.modname = {rel: name for name, rel in files.items()}
        self.rdeps = self._reverse_deps()
        self.import_cpu = time.process_time() - t0  # what a full re-import costs here
        self.xrefs = self._cross_references()

    def _cross_references(self):
        """Entries of module-level and class-level containers that hold a class or
        function defined in another sqlglot module: registries filled at import
        time (the dialect table, for one). When that other module is re-imported,
        entries left behind in a module that is not would keep the old objects
        alive under their old names."""
        out = {}

        def scan(owner, box):
            if isinstance(box, dict):
                items = list(box.items())
            elif isinstance(box, (list, set)):
                items = [(None, v) for v in list(box)]
            else:
                return
            for k, v in items:
                if isinstance(v, (type, types.FunctionType)):
                    m = getattr(v, "__module__", None)
                    if m and m != owner and m in self.files:
                        out.setdefault(m, []).append((owner, box, k, v))

        seen = set()
        for name, mod in list(sys.modules.items()):
            if mod is None or name not in self.files or not name.startswith("sqlglot"):
                continue
            stack = []
            for v in list(vars(mod).values()):
                if isinstance(v, (dict, list, set)):
                    scan(name, v)
                elif isinstance(v, type) and getattr(v, "__module__", None) == name:
                    stack.append(v)
            while stack:
                c = stack.pop()
                if id(c) in seen:
                    continue
                seen.add(id(c))
                for v in list(vars(c).values()):
                    if isinstance(v, (dict, list, set)):
                        scan(name, v)
                    elif isinstance(v, type) and getattr(v, "__module__", None) == name:
                        stack.append(v)
                meta = type(c)
                if meta is not type and getattr(meta, "__module__", None) == name and id(meta) not in seen:
                    stack.append(meta)
        return out

    def _reverse_deps(self):
        deps = {m: set(d) for m, d in self.maps.head["deps"]["deps"].items()}
        # what a module's namespace holds after import, beyond what it names in
        # import statements; modules with a lazy __getattr__ are fixed up on purge
        for name, mod in list(sys.modules.items()):
            if mod is None or not name.startswith("sqlglot") or name not in self.files:
                continue
            ns = vars(mod)
            if "__getattr__" in ns:
                continue
            d = deps.setdefault(name, set())
            for v in list(ns.values()):
                if isinstance(v, types.ModuleType):
                    m = v.__name__
                    if m.startswith(name + "."):
                        continue  # a package's own submodule, bound by the import system
                elif isinstance(v, (type, types.FunctionType)):
                    m = getattr(v, "__module__", None)
                else:
                    continue
                if m and m != name and m in self.files:
                    d.add(m)
        r = {}
        for m, ds in deps.items():
            for d in ds:
                r.setdefault(d, set()).add(m)
        return r

    def dependents(self, mods, prefix="sqlglot"):
        out, stack = set(), list(mods)
        while stack:
            m = stack.pop()
            if m in out:
                continue
            out.add(m)
            for d in self.rdeps.get(m, ()):
                if d.startswith(prefix) and d not in out:
                    stack.append(d)
            # a package is imported before its submodules, so it depends on nothing here
        return out


class Overlay(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves patched source for the modules a patch touches."""

    def __init__(self, src, sources):
        self.src = src
        self.sources = sources  # module name -> (path, source text or None when deleted)

    def find_spec(self, name, path=None, target=None):
        hit = self.sources.get(name)
        if hit is None:
            return None
        rel, text = hit
        if text is None:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        full = os.path.join(self.src, rel)
        is_pkg = rel.endswith("__init__.py")
        return importlib.util.spec_from_file_location(
            name, full, loader=self, submodule_search_locations=[os.path.dirname(full)] if is_pkg else None
        )

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        rel, text = self.sources[module.__name__]
        full = os.path.join(self.src, rel)
        import linecache

        linecache.cache[full] = (len(text), None, text.splitlines(True), full)
        code = compile(text, full, "exec", dont_inherit=True)
        exec(code, module.__dict__)


def reload_patched(warm, sources, extra=()):
    """Re-import the patched modules and their dependents.

    Returns (old namespaces, purged module names in import order, broken modules).
    """
    changed = [m for m in sources]
    purge = warm.dependents([m for m in changed if m in warm.files] + list(extra))
    purge.update(changed)
    purge = {m for m in purge if not m.endswith("__main__")}
    order = [m for m in sys.modules if m in purge]
    order += [m for m in purge if m not in sys.modules]
    old = {m: dict(vars(sys.modules[m])) for m in order if m in sys.modules}
    old_mods = {m: sys.modules[m] for m in order if m in sys.modules}
    stale = set(id(v) for v in old_mods.values())
    for m in order:
        sys.modules.pop(m, None)
        pkg, _, leaf = m.rpartition(".")
        p = sys.modules.get(pkg)
        if p is not None and leaf in vars(p):
            try:
                delattr(p, leaf)
            except Exception:
                pass
    # lazily filled namespaces must forget what they cached from purged modules
    for name, mod in list(sys.modules.items()):
        if mod is None or not name.startswith(("sqlglot", "tests")):
            continue
        ns = vars(mod)
        if "__getattr__" not in ns:
            continue
        for k, v in list(ns.items()):
            if id(v) in stale or getattr(v, "__module__", None) in purge:
                if not k.startswith("__"):
                    del ns[k]
    # registries in modules that stay loaded must not keep objects of purged ones
    for m in order:
        for owner, box, k, v in warm.xrefs.get(m, ()):
            if owner in purge:
                continue
            try:
                if isinstance(box, dict):
                    if box.get(k) is v:
                        del box[k]
                elif isinstance(box, list):
                    while v in box:
                        box.remove(v)
                else:
                    box.discard(v)
            except Exception:
                pass
    sys.meta_path.insert(0, Overlay(warm.src, sources))
    importlib.invalidate_caches()
    broken = {}
    for m in order:
        if m in sys.modules:
            continue
        try:
            importlib.import_module(m)
        except BaseException as e:  # noqa: a patch may raise anything at import
            broken[m] = repr(e)[:200]
            sys.modules.pop(m, None)
    # a re-executed package has lost the attributes for submodules that stayed loaded
    for m in order:
        pkg = sys.modules.get(m)
        if pkg is None or not hasattr(pkg, "__path__"):
            continue
        prefix = m + "."
        for name, sub in list(sys.modules.items()):
            if sub is not None and name.startswith(prefix) and "." not in name[len(prefix):]:
                if name[len(prefix):] not in vars(pkg):
                    setattr(pkg, name[len(prefix):], sub)
    return old, order, broken, old_mods


# ---------------------------------------------------------------- comparing
def code_same(a, b):
    if a is b:
        return True
    if (
        a.co_code != b.co_code
        or a.co_names != b.co_names
        or a.co_varnames != b.co_varnames
        or a.co_freevars != b.co_freevars
        or a.co_cellvars != b.co_cellvars
        or a.co_argcount != b.co_argcount
        or a.co_kwonlyargcount != b.co_kwonlyargcount
        or a.co_posonlyargcount != b.co_posonlyargcount
        or a.co_flags != b.co_flags
        or a.co_exceptiontable != b.co_exceptiontable
        or a.co_name != b.co_name
        or len(a.co_consts) != len(b.co_consts)
    ):
        return False
    for x, y in zip(a.co_consts, b.co_consts):
        if isinstance(x, types.CodeType):
            if not isinstance(y, types.CodeType) or not code_same(x, y):
                return False
        elif type(x) is not type(y) or x != y or repr(x) != repr(y):
            return False
    return True


_own = {}


def _own_names(t):
    r = _own.get(t)
    if r is None:
        r = _own[t] = {n for c in t.__mro__ if c is not object for n in vars(c)}
    return r


def _key(k):
    if isinstance(k, PLAIN):
        return k
    return knorm(k)


def _plain_function(f):
    """A function written in a source file, with no closure and wrapping nothing."""
    return (
        f.__closure__ is None
        and "__wrapped__" not in f.__dict__
        and f.__code__.co_filename.endswith(".py")
        and not f.__code__.co_filename.startswith("<")
    )


class Cmp:
    """Structural equality between objects built before and after a reload."""

    def __init__(self):
        self.class_pairs = []
        self.default_pairs = []  # functions that differ only in default values
        self.seen = {}
        self.keep = []  # keeps compared objects alive so ids stay unique

    def same(self, a, b, d=0):
        """Results are remembered per pair; a pair met again while it is still being
        compared (a cycle) counts as equal."""
        if a is b:
            return True
        ta, tb = type(a), type(b)
        if isinstance(a, type) or isinstance(b, type):
            if isinstance(a, type) and isinstance(b, type) and cname(a) == cname(b):
                self.class_pairs.append((a, b))  # same name; their contents are compared separately
                return True
            return False
        if ta is not tb and cname(ta) != cname(tb):
            return False
        if ta in PLAIN:
            return a == b
        if isinstance(a, types.ModuleType):
            return a.__name__ == b.__name__
        if isinstance(a, enum.Enum):
            return a.name == b.name
        pair = (id(a), id(b))
        hit = self.seen.get(pair)
        if hit is not None:
            return hit
        if d > 40:
            return self._eq(a, b)
        if ta.__module__.startswith("sqlglot") and "__repr__" in _own_names(ta):
            # sqlglot's own value objects (expression trees above all) print their full state
            ra = repr(a)
            if " at 0x" not in ra:
                r = ra == repr(b)
                self.seen[pair] = r
                self.keep.append((a, b))
                return r
        self.seen[pair] = True
        self.keep.append((a, b))
        r = self._same(a, b, d)
        self.seen[pair] = r
        return r

    def _same(self, a, b, d):
        ta = type(a)
        if isinstance(a, (tuple, list)):
            if len(a) != len(b):
                return False
            if all(self.same(x, y, d + 1) for x, y in zip(a, b)):
                return True
            # a tuple of classes made from a set comes out in a different order every import
            if ta is tuple and all(isinstance(x, type) for x in a) and all(isinstance(y, type) for y in b):
                return sorted(map(cname, a)) == sorted(map(cname, b))
            return False
        if isinstance(a, dict):
            if len(a) != len(b):
                return False
            try:
                kb = {_key(k): v for k, v in b.items()}
                for k, v in a.items():
                    kk = _key(k)
                    if kk not in kb or not self.same(v, kb[kk], d + 1):
                        return False
                return True
            except TypeError:
                return self._eq(a, b)
        if isinstance(a, (set, frozenset)):
            try:
                return {_key(k) for k in a} == {_key(k) for k in b}
            except TypeError:
                return self._eq(a, b)
        if isinstance(a, types.FunctionType):
            return self.fn_same(a, b, d)
        if isinstance(a, (staticmethod, classmethod)):
            return self.same(a.__func__, b.__func__, d + 1)
        if isinstance(a, property):
            return (
                self.same(a.fget, b.fget, d + 1)
                and self.same(a.fset, b.fset, d + 1)
                and self.same(a.fdel, b.fdel, d + 1)
            )
        if isinstance(a, functools.partial):
            return (
                self.same(a.func, b.func, d + 1)
                and self.same(a.args, b.args, d + 1)
                and self.same(a.keywords, b.keywords, d + 1)
            )
        if isinstance(a, types.MethodType):
            return self.same(a.__func__, b.__func__, d + 1) and self.same(a.__self__, b.__self__, d + 1)
        if isinstance(a, re.Pattern):
            return a.pattern == b.pattern and a.flags == b.flags
        if isinstance(a, (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
                          types.GetSetDescriptorType, types.MemberDescriptorType)):
            return getattr(a, "__qualname__", None) == getattr(b, "__qualname__", None)
        return self.obj_same(a, b, d)

    def _eq(self, a, b):
        try:
            return bool(a == b)
        except Exception:
            return False

    def fn_same(self, f, g, d):
        if not code_same(f.__code__, g.__code__):
            return False
        if self.fn_env_same(f, g, d):
            return True
        if _plain_function(f) and _plain_function(g):
            # the same body with other default values: nothing but a call can tell
            # them apart, and the tests that make one are found by its lines
            self.default_pairs.append((f, g))
            return True
        return False

    def fn_env_same(self, f, g, d=0):
        if not self.same(f.__defaults__, g.__defaults__, d + 1):
            return False
        if not self.same(f.__kwdefaults__, g.__kwdefaults__, d + 1):
            return False
        cf, cg = f.__closure__ or (), g.__closure__ or ()
        if len(cf) != len(cg):
            return False
        for x, y in zip(cf, cg):
            try:
                vx = x.cell_contents
            except ValueError:
                vx = Cmp
            try:
                vy = y.cell_contents
            except ValueError:
                vy = Cmp
            if not self.same(vx, vy, d + 1):
                return False
        wf, wg = f.__dict__.get("__wrapped__"), g.__dict__.get("__wrapped__")
        if (wf is None) != (wg is None):
            return False
        if wf is not None and not self.same(wf, wg, d + 1):
            return False
        return True

    def obj_same(self, a, b, d):
        sa, sb = _state(a), _state(b)
        if sa is None or sb is None:
            if self._eq(a, b):
                return True
            ra, rb = repr(a), repr(b)
            return ra == rb and " at 0x" not in ra
        if sa.keys() != sb.keys():
            return False
        for k, v in sa.items():
            if not self.same(v, sb[k], d + 1):
                return False
        return True


def _state(o):
    out = {}
    found = False
    d = getattr(o, "__dict__", None)
    if isinstance(d, dict):
        out.update(d)
        found = True
    for c in type(o).__mro__:
        slots = c.__dict__.get("__slots__")
        if slots is None:
            continue
        if isinstance(slots, str):
            slots = (slots,)
        for s in slots:
            if s in ("__dict__", "__weakref__"):
                continue
            found = True
            try:
                out[s] = getattr(o, s)
            except AttributeError:
                out[s] = Cmp
    if not found and type(o).__module__ in ("builtins",):
        return None
    return out if found else None


def _unwrap(v):
    if isinstance(v, (staticmethod, classmethod)):
        return v.__func__
    return v


def _innermost(f):
    """The function a chain of decorators finally calls: follows __wrapped__, then
    functions held in closure cells. A decorator's wrapper is shared by everything
    it decorates, so its own lines say nothing about one decorated function."""
    seen = set()
    while isinstance(f, types.FunctionType) and id(f) not in seen:
        seen.add(id(f))
        nxt = f.__dict__.get("__wrapped__")
        if not isinstance(_unwrap(nxt), types.FunctionType):
            nxt = None
            for cell in f.__closure__ or ():
                try:
                    v = _unwrap(cell.cell_contents)
                except ValueError:
                    continue
                if isinstance(v, types.FunctionType):
                    if nxt is not None:
                        return f  # more than one candidate: stay with the wrapper
                    nxt = v
        if nxt is None:
            return f
        f = _unwrap(nxt)
    return f


class Diff:
    """What a reload changed, as test sets."""

    def __init__(self, warm, patched_modules):
        self.warm = warm
        self.maps = warm.maps
        self.patched = set(patched_modules)
        self.bits = 0
        self.notes = []
        self.cmp = Cmp()
        self.done = set()
        self.purged = set()  # modules that were re-imported

    # -- map lookups
    def subs(self, c):
        return self.maps.subclasses(c)

    def attr_readers(self, c, attr):
        bits = 0
        for x in self.subs(c):
            bits |= self.maps.attr_reads(x).get(attr, 0)
        return bits

    def users(self, c):
        bits = 0
        u = self.maps.head["users"]
        for x in self.subs(c):
            bits |= u.get(x, 0)
        return bits

    def key_readers(self, label, keys):
        kr = self.maps.key_reads(label)
        if kr is None:
            return None
        bits = kr.get(ALL, 0)
        for k in keys:
            bits |= kr.get(k, 0)
        return bits

    def cover_code(self, code, unknown=0):
        """Tests that run the function. `unknown` is returned for code that has no
        source file in the tree (generated, as dataclass methods are)."""
        rel = os.path.relpath(code.co_filename, self.warm.src)
        if rel.startswith("..") or not rel.endswith(".py"):
            return unknown
        lines = self.maps.lines(rel)
        if not lines:
            return 0
        lo, hi = _code_span(code)
        bits = 0
        for ln in range(lo, hi + 1):
            b = lines.get(ln)
            if b:
                bits |= b
        return bits

    # -- comparing namespaces
    def module(self, name, old, new):
        for k in old.keys() | new.keys():
            if k in SKIP:
                continue
            a, b = old.get(k, Diff), new.get(k, Diff)
            if a is b:
                continue
            if isinstance(a, type) and getattr(a, "__module__", None) == name:
                if isinstance(b, type) and cname(a) == cname(b):
                    self.klass(a, b)
                    continue
                # the class is gone or replaced: whoever builds or reads it can tell
                self.notes.append(f"class removed {cname(a)}")
                _class_everything(self, a)
                self.global_changed(name, k, a, b, old)
                continue
            if isinstance(a, types.FunctionType) and isinstance(b, types.FunctionType):
                if a.__module__ == name and b.__module__ == name:
                    if code_same(a.__code__, b.__code__):
                        if self.cmp.fn_env_same(a, b):
                            continue
                        if _plain_function(a) and _plain_function(b):
                            self.cmp.default_pairs.append((a, b))  # only its default values changed
                            continue
                    elif name in self.patched and self.cmp.fn_env_same(a, b):
                        continue  # a body edit: line coverage already selects for it
                    self.bits |= self.cover_code(_innermost(a).__code__)
                    self.global_changed(name, k, a, b, old)
                    continue
                if code_same(a.__code__, b.__code__) or cname(a) == cname(b):
                    continue  # imported from elsewhere; reported at home
            if a is Diff or b is Diff or not self.cmp.same(a, b):
                self.global_changed(name, k, a, b, old)

    def global_changed(self, mod, name, a, b, old_ns):
        self.notes.append(f"global {mod}::{name}")
        keys = _changed_keys(a, b, self.cmp)
        if keys is not None:
            r = self.key_readers(f"{mod}::{name}", keys)
            if r is not None:
                self.bits |= r
                return
        # no read record for this name: every function that can reach it
        self.name_refs.setdefault(name, []).append((mod, a))

    def klass(self, a, b):
        ca = cname(a)
        if (id(a), id(b)) in self.done:
            return
        self.done.add((id(a), id(b)))
        if [cname(x) for x in a.__bases__] != [cname(x) for x in b.__bases__]:
            self.notes.append(f"bases {ca}")
            self.bits |= self.users(ca)
        da, db = vars(a), vars(b)
        if da.get("__slots__") != db.get("__slots__"):
            self.notes.append(f"slots {ca}")
            self.bits |= self.users(ca)
        for k in da.keys() | db.keys():
            if k in SKIP or k == "__slots__":
                continue
            x, y = da.get(k, Diff), db.get(k, Diff)
            if x is y:
                continue
            if isinstance(x, type) and isinstance(y, type) and cname(x) == cname(y):
                # a class held as an attribute: a nested one, or one a metaclass made on
                # the fly (the default tokenizer of a dialect), which no module names
                if x is not y and getattr(x, "__module__", None) in self.purged:
                    self.klass(x, y)
                continue
            fx, fy = _unwrap(x), _unwrap(y)
            if isinstance(fx, types.FunctionType) or isinstance(fy, types.FunctionType):
                self.method(a, ca, k, x, y, fx, fy)
                continue
            if isinstance(x, property) or isinstance(y, property):
                if x is not Diff and y is not Diff and isinstance(x, property) and isinstance(y, property):
                    if self._prop_same(a, x, y):
                        continue
                self.notes.append(f"property {ca}.{k}")
                for f in (getattr(x, "fget", None), getattr(x, "fset", None)):
                    if isinstance(f, types.FunctionType):
                        self.bits |= self.cover_code(f.__code__)
                if not isinstance(x, property):
                    self.bits |= self.attr_readers(ca, k) or self.users(ca)
                continue
            if x is Diff or y is Diff:
                self.notes.append(f"attr+- {ca}.{k}")
                self.bits |= self.attr_readers(ca, k)
                if y is not Diff and x is Diff:
                    # a new name: whoever reads it by a computed name is not on record
                    self.bits |= self.users(ca)
                continue
            if self.cmp.same(x, y):
                continue
            keys = _changed_keys(x, y, self.cmp)
            if keys is not None:
                r = self.key_readers(f"{ca}.{k}", keys)
                if r is not None:
                    self.notes.append(f"keys {ca}.{k} {sorted(keys)[:4]}")
                    self.bits |= r
                    continue
            self.notes.append(f"attr {ca}.{k}")
            self.bits |= self.attr_readers(ca, k)

    def _prop_same(self, klass, x, y):
        for f, g in ((x.fget, y.fget), (x.fset, y.fset), (x.fdel, y.fdel)):
            if (f is None) != (g is None):
                return False
            if f is None:
                continue
            if not isinstance(f, types.FunctionType) or not isinstance(g, types.FunctionType):
                if not self.cmp.same(f, g):
                    return False
                continue
            if not self.cmp.fn_env_same(f, g):
                return False
            if not code_same(f.__code__, g.__code__) and klass.__module__ not in self.patched:
                return False
        return True

    def method(self, klass, ca, k, x, y, fx, fy):
        if x is not Diff and y is not Diff and type(x) is type(y) and isinstance(fx, types.FunctionType) and isinstance(fy, types.FunctionType):
            if self.cmp.fn_env_same(fx, fy):
                if code_same(fx.__code__, fy.__code__) or klass.__module__ in self.patched:
                    return  # unchanged, or a body edit that line coverage selects for
        self.notes.append(f"method {ca}.{k}")
        if isinstance(fx, types.FunctionType):
            hit = self.cover_code(_innermost(fx).__code__, None)
            # a generated method (a dataclass __init__ with a new default, say):
            # no lines to go by, so everyone who builds the class
            self.bits |= self.users(ca) if hit is None else hit
        if x is Diff:
            # a method the class did not have: it now hides the inherited one
            base = None
            for c in klass.__mro__[1:]:
                if k in vars(c):
                    base = _unwrap(vars(c)[k])
                    break
            users = self.users(ca)
            if isinstance(base, types.FunctionType):
                self.bits |= self.cover_code(base.__code__) & users
            else:
                self.bits |= users
        elif not isinstance(fx, types.FunctionType):
            self.bits |= self.attr_readers(ca, k) or self.users(ca)

    name_refs = None


def _changed_keys(a, b, cmp):
    """Normalized names of the entries that differ between two dicts or two sets."""
    try:
        if isinstance(a, dict) and isinstance(b, dict):
            ka = {_key(k): (k, v) for k, v in a.items()}
            kb = {_key(k): (k, v) for k, v in b.items()}
            out = set()
            for kk in ka.keys() | kb.keys():
                if kk not in ka:
                    out.add(knorm(kb[kk][0]))
                elif kk not in kb:
                    out.add(knorm(ka[kk][0]))
                elif not cmp.same(ka[kk][1], kb[kk][1]):
                    out.add(knorm(ka[kk][0]))
            return out
        if isinstance(a, (set, frozenset)) and isinstance(b, (set, frozenset)):
            ka = {_key(k): k for k in a}
            kb = {_key(k): k for k in b}
            return {knorm((ka.get(kk, None) if kk in ka else kb[kk])) for kk in ka.keys() ^ kb.keys()}
    except TypeError:
        return None
    return None


def _code_span(code):
    lo = code.co_firstlineno
    hi = lo
    stack = [code]
    while stack:
        c = stack.pop()
        for _, _, ln in c.co_lines():
            if ln is not None and ln > hi:
                hi = ln
        for k in c.co_consts:
            if isinstance(k, types.CodeType):
                stack.append(k)
    return lo, hi


def _codes_of(ns, modname):
    """Every code object defined by a module namespace."""
    out = []
    seen = set()
    stack = [v for v in ns.values()]
    while stack:
        v = stack.pop()
        if id(v) in seen:
            continue
        seen.add(id(v))
        v = _unwrap(v)
        if isinstance(v, types.FunctionType):
            if v.__module__ == modname:
                out.append(v.__code__)
        elif isinstance(v, property):
            stack.extend(f for f in (v.fget, v.fset, v.fdel) if f is not None)
        elif isinstance(v, type) and getattr(v, "__module__", None) == modname:
            stack.extend(vars(v).values())
    return out


def _mentions(code, names):
    stack = [code]
    while stack:
        c = stack.pop()
        if names.intersection(c.co_names):
            return True
        stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
    return False


def state_diff(warm, sources, diff=True):
    """Tests that can see what importing the patched `sources` changes.

    With diff=False the patched modules are only loaded (to run tests against
    them) and just broken imports are reported.
    Returns (bits, notes, broken modules, old namespaces).
    """
    old, order, broken, old_mods = reload_patched(warm, sources)
    d = Diff(warm, sources.keys())
    d.name_refs = {}
    d.purged = set(order)
    maps = warm.maps
    for m in order if diff else ():
        if not m.startswith("sqlglot"):
            continue
        if m in broken or m not in sys.modules:
            continue
        if m not in old:
            continue
        d.module(m, old[m], vars(sys.modules[m]))
    # classes met inside containers and objects (not named by a module or held as a
    # class attribute) are compared too, as long as their module was re-imported
    i = 0
    pairs = d.cmp.class_pairs
    while i < len(pairs):
        x, y = pairs[i]
        i += 1
        if x is not y and getattr(x, "__module__", None) in d.purged:
            d.klass(x, y)
    done = set()
    for f, g in d.cmp.default_pairs:
        if id(f) not in done:
            done.add(id(f))
            d.notes.append(f"defaults {f.__module__}::{f.__qualname__}")
            d.bits |= d.cover_code(f.__code__)
    if d.name_refs:
        # Globals with no read record: select by the functions that mention the name,
        # in modules where that name is bound to the changed object itself (the home
        # module or a `from x import name`), or that hold the home module and may
        # reach the name as an attribute of it.
        for m in order:
            ns = old.get(m)
            if ns is None:
                continue
            names = set()
            held = {id(v) for v in ns.values() if isinstance(v, types.ModuleType)}
            for name, homes in d.name_refs.items():
                for home, obj in homes:
                    if m == home or (obj is not Diff and ns.get(name, Diff) is obj):
                        names.add(name)
                    elif id(old_mods.get(home)) in held:
                        names.add(name)
            if not names:
                continue
            for code in _codes_of(ns, m):
                if _mentions(code, names):
                    d.bits |= d.cover_code(code)
    for m in broken:
        d.notes.append(f"broken {m}: {broken[m]}")
        rel = warm.files.get(m)
        if rel:
            for b in maps.lines(rel).values():
                d.bits |= b
        ns = old.get(m, {})
        for v in ns.values():
            if isinstance(v, type) and getattr(v, "__module__", None) == m:
                _class_everything(d, v)
        d.bits |= maps.tests_importing(m)
    return d.bits, d.notes, broken, old


def _class_everything(d, c, seen=None):
    seen = seen if seen is not None else set()
    if id(c) in seen:
        return
    seen.add(id(c))
    ca = cname(c)
    d.bits |= d.users(ca)
    for x in d.subs(ca):
        for b in d.maps.attr_reads(x).values():
            d.bits |= b
    for v in vars(c).values():
        if isinstance(v, type) and getattr(v, "__module__", None) == c.__module__:
            _class_everything(d, v, seen)


# ---------------------------------------------------------------- patching in place
def function_index(src):
    """Every sqlglot function object alive in this process, by where its code
    was defined.  Built once; lets a child swap the code of an edited function
    without importing anything again."""
    import gc

    prefix = os.path.join(src, "sqlglot") + os.sep
    index = {}
    for o in gc.get_objects():
        if type(o) is types.FunctionType:
            c = o.__code__
            if c.co_filename.startswith(prefix):
                index.setdefault((c.co_filename, c.co_qualname, c.co_firstlineno), []).append(o)
    return index


def swap_code(warm, rel, old_text, new_text):
    """Give every function the patch edits its new code, in the loaded sqlglot.

    Only for patches that change function bodies and nothing that runs at import
    time: the two versions of the file must define the same functions in the
    same places.  Returns False (having changed nothing) when that does not
    hold or a function cannot be matched for certain."""
    full = os.path.join(warm.src, rel)
    try:
        old = compile(old_text, full, "exec", dont_inherit=True)
        new = compile(new_text, full, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    pairs = []
    seen = {}

    def walk(a, b):
        ca = [k for k in a.co_consts if isinstance(k, types.CodeType)]
        cb = [k for k in b.co_consts if isinstance(k, types.CodeType)]
        if len(ca) != len(cb):
            return False
        for x, y in zip(ca, cb):
            if x.co_name != y.co_name:
                return False
            key = (x.co_qualname, x.co_firstlineno)
            seen[key] = seen.get(key, 0) + 1
            if not code_same(x, y):
                pairs.append((key, x, y))
            if not walk(x, y):
                return False
        return True

    if not walk(old, new):
        return False
    todo = []
    for key, x, y in pairs:
        if seen[key] != 1 or x.co_freevars != y.co_freevars:
            return False  # two functions on one line, or a different closure shape
        for f in warm.findex.get((full,) + key, ()):
            if len(f.__code__.co_freevars) != len(y.co_freevars):
                return False
            todo.append((f, y))
    for f, y in todo:
        f.__code__ = y
    return True
