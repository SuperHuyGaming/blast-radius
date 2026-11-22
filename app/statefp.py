"""Fingerprint of what importing sqlglot leaves behind: the value of every module global and of
every class attribute. Two imports (pinned tree, patched tree) are compared key by key; a
patch that changes import-time code can only matter through the keys that differ."""
from __future__ import annotations

import enum
import marshal
import os
import re
import sys
import types

_PRIMITIVE = (str, int, float, bool, bytes, complex, type(None))
_MARSHAL_OK = (dict, list, tuple, set, frozenset)


_PRIM_NAME = {t: t.__name__ for t in _PRIMITIVE}
_FUNCTION = types.FunctionType
_CODE = types.CodeType
_NONE_FP = ("NoneType", None)
_FUNCTION_TYPES = frozenset((types.FunctionType, classmethod, staticmethod, property, types.MethodType))
_TABLE_TYPES = frozenset((dict, set, frozenset))
# builtin types whose __module__ is "builtins" (never "sqlglot..."), so snapshot() skips the lookup
_PLAIN = frozenset((dict, list, tuple, set, frozenset, types.FunctionType, classmethod, staticmethod, property,
                    types.MethodType, type, types.BuiltinFunctionType))


def _load_c():
    """The C version of Fingerprinter.fp (native/_statefpc.c, built into the image next to this
    file): the same walk building the same tuples, so the same fingerprints; the build checks
    that on the pinned state (build_cfp_check.py) and removes the module on any difference.
    None (the Python code runs) when it is missing, fails to load, or BR_NO_CFP=1."""
    if os.environ.get("BR_NO_CFP") == "1":
        return None
    try:
        import importlib.machinery
        import importlib.util

        here = os.path.dirname(os.path.abspath(__file__))
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            path = os.path.join(here, "_statefpc" + suffix)
            if os.path.exists(path):
                spec = importlib.util.spec_from_file_location("_statefpc", path)
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)
                m.setup(_PRIM_NAME, enum.Enum, marshal.dumps)
                return m
    except Exception:  # noqa: BLE001  (no C walk: the Python one)
        pass
    return None


_cfp = _load_c()


class Fingerprinter:
    def __init__(self, code_alias=None, base_codes=None):
        self.memo = {}
        self.busy = set()
        self.code_memo = {}
        self.code_objects = []  # every code object of code_memo, so that its id stays its own
        # code objects that count as equal to another one (a function whose body the patch changed)
        self.code_alias = code_alias or {}
        # {id(code): hash} of another snapshot in this process (see base_codes()): a code
        # object that it hashed gets the same hash here. Not when the alias maps one of those
        # hashes to another: the hash of a code object holds the hashes of its nested ones.
        self.base = None
        if base_codes:
            values = base_codes[1]
            if not any(k != v and k in values for k, v in self.code_alias.items()):
                self.base = base_codes[0]
        if _cfp is not None and type(self.code_alias) is dict and (self.base is None or type(self.base) is dict):
            # fp and code in C (instance attributes, so self.fp, f.fp, f.code and the recursion
            # of the branches left to Python all use them); they share memo, busy, code_memo,
            # code_objects, base and code_alias with the Python code
            self.fp = _cfp.FP(self, self.memo, self.busy, self.code_memo, self.base, self.code_alias, self.code_objects)
            self.code = self.fp.code
            # (the Python branches that the C fp calls, _fp_plain, _fp and _fp_rest, then look up the
            # memo of the C walk in _seq and _dict, as the Python ones look up self.memo)
            self._seq = self.fp.seq
            self._dict = self.fp.dict

    def base_codes(self):
        """What a later Fingerprinter needs to reuse the code hashes of this one (base_codes=).
        Holds the code objects, so that their ids are not reused."""
        return dict(self.code_memo), frozenset(self.code_memo.values()), list(self.code_objects)

    def code(self, c):
        h = self.code_memo.get(id(c))
        if h is None and self.base is not None:
            h = self.base.get(id(c))
            if h is not None:
                h = self.code_alias.get(h, h)
                self.code_memo[id(c)] = h
                return h
        if h is None:
            self.code_objects.append(c)
            parts = [c.co_code, c.co_names, c.co_varnames, c.co_freevars, c.co_argcount, c.co_flags]
            for k in c.co_consts:
                parts.append(self.code(k) if isinstance(k, types.CodeType) else (type(k).__name__, repr(k)))
            h = hash(tuple(parts))
            h = self.code_alias.get(h, h)
            self.code_memo[id(c)] = h
        return h

    def fp(self, v, depth=0):
        t = type(v)
        n = _PRIM_NAME.get(t)
        if n is not None:
            return (n, v)
        key = id(v)
        got = self.memo.get(key)
        if got is not None:
            return got
        busy = self.busy
        if key in busy or depth > 12:
            return ("cycle", t.__name__)
        if t in _MARSHAL_OK:
            # (as _fp's first step; marshal.dumps calls no fp, so v need not be in busy)
            try:
                out = hash(("m", t.__name__, marshal.dumps(v, 2)))
            except ValueError:
                pass
            else:
                self.memo[key] = out
                return out
            busy.add(key)
            try:
                out = self._fp_plain(v, t, depth)
            finally:
                busy.discard(key)
        elif t is _FUNCTION:
            busy.add(key)
            try:
                out = self._fp_function(v, depth)
            finally:
                busy.discard(key)
        else:
            busy.add(key)
            try:
                out = self._fp(v, t, depth)
            finally:
                busy.discard(key)
        try:
            out = hash(out)
        except TypeError:
            out = hash(repr(out))
        self.memo[key] = out
        return out

    def _fp_head(self, v, t):
        """fp's step after the memo and busy tests, for the C fp when the metaclass of t compares
        or hashes in its own way (its __eq__ / __hash__ then run here, as in fp): None when t is
        not in _MARSHAL_OK (fp calls _fp next), False when it is and marshal raised ValueError
        (fp calls _fp_plain next), else the hash that fp stores."""
        if t in _MARSHAL_OK:
            try:
                return hash(("m", t.__name__, marshal.dumps(v, 2)))
            except ValueError:
                return False
        return None

    def _fp_plain(self, v, t, depth):
        """_fp of a dict, list, tuple, set or frozenset that marshal cannot write."""
        if t is dict:
            return self._dict(v, depth)
        if t is tuple and v and all(isinstance(x, type) for x in v):
            return ("tc", frozenset(self._seq(v, depth + 1)))
        if t in (list, tuple):
            return (t.__name__, tuple(self._seq(v, depth + 1)))
        return ("s", frozenset(self._seq(v, depth + 1)))

    def _fp_function(self, v, depth):
        """_fp of a plain function (type(v) is types.FunctionType), with fp's cheap cases inlined."""
        cells = ()
        if v.__closure__:
            cells = tuple(self._cell(c, depth) for c in v.__closure__)
        code = v.__code__
        h = self.code_memo.get(id(code))
        if h is None:
            h = self.code(code)
        d = v.__defaults__
        k = v.__kwdefaults__
        return ("F", v.__qualname__, h, _NONE_FP if d is None else self.fp(d, depth + 1),
                _NONE_FP if k is None else self.fp(k, depth + 1), cells)

    def _fp(self, v, t, depth):
        if t in _MARSHAL_OK:
            try:
                # plain data: fast path. Version 2 writes no object references, so the bytes
                # do not depend on how many references an object happens to have (the code
                # cache and cone.py's record add some)
                return ("m", t.__name__, marshal.dumps(v, 2))
            except ValueError:
                pass
            if t is dict:
                return self._dict(v, depth)
            if t is tuple and v and all(isinstance(x, type) for x in v):
                # a tuple of classes (isinstance checks) is often made from a set: no order
                return ("tc", frozenset(self._seq(v, depth + 1)))
            if t in (list, tuple):
                return (t.__name__, tuple(self._seq(v, depth + 1)))
            return ("s", frozenset(self._seq(v, depth + 1)))
        if isinstance(v, enum.Enum):
            return ("E", t.__module__, t.__qualname__, v.name, self.fp(v.value, depth + 1))
        if isinstance(v, type):
            return ("C", v.__module__, v.__qualname__)
        if t is types.ModuleType:
            return ("M", v.__name__)
        if t is types.FunctionType:
            cells = ()
            if v.__closure__:
                cells = tuple(self._cell(c, depth) for c in v.__closure__)
            return ("F", v.__qualname__, self.code(v.__code__), self.fp(v.__defaults__, depth + 1),
                    self.fp(v.__kwdefaults__, depth + 1), cells)
        if t in (staticmethod, classmethod):
            return (t.__name__, self.fp(v.__func__, depth + 1))
        if t is property:
            return ("prop", self.fp(v.fget, depth + 1), self.fp(v.fset, depth + 1), self.fp(v.fdel, depth + 1))
        if t is types.MethodType:
            return ("meth", self.fp(v.__func__, depth + 1), self.fp(v.__self__, depth + 1))
        return self._fp_rest(v, t, depth)

    def _fp_rest(self, v, t, depth):
        """The branches of _fp after the method one (the C fp calls this one for them)."""
        if t is types.BuiltinFunctionType or t is types.MethodDescriptorType or t is types.WrapperDescriptorType:
            return ("B", getattr(v, "__qualname__", repr(v)))
        if t is re.Pattern:
            return ("re", v.pattern, v.flags)
        if isinstance(v, dict):
            return ("dd", t.__qualname__, self._dict(v, depth))
        if isinstance(v, (list, tuple)):
            return ("ll", t.__qualname__, tuple(self.fp(x, depth + 1) for x in v))
        if isinstance(v, (set, frozenset)):
            return ("ss", t.__qualname__, frozenset(self.fp(x, depth + 1) for x in v))
        mod = getattr(t, "__module__", "") or ""
        if mod.startswith("functools") and hasattr(v, "func"):
            return ("partial", self.fp(v.func, depth + 1), self.fp(v.args, depth + 1), self.fp(v.keywords, depth + 1))
        if mod.startswith("sqlglot"):
            state = self._state(v, t)
            if state is not None:
                return ("O", mod, t.__qualname__, tuple((k, self.fp(x, depth + 1)) for k, x in state))
        wrapped = getattr(v, "__wrapped__", None)
        if wrapped is not None:
            return ("W", mod, t.__qualname__, self.fp(wrapped, depth + 1))
        # An object of another library. A value object (a date, a decimal) prints its value;
        # an object that prints its address (a logger's lock, a cache) has no value to compare.
        try:
            text = repr(v)
        except Exception:  # noqa: BLE001
            text = " at 0x"
        if " at 0x" in text or len(text) > 4000:
            return ("T", mod, t.__qualname__)
        return ("R", mod, t.__qualname__, text)

    def _seq(self, v, d):
        """[self.fp(x, d) for x in v], with the cheap cases (plain values, objects seen
        before) inlined: the same values, computed in the same order."""
        prim = _PRIM_NAME.get
        memo = self.memo.get
        fp = self.fp
        out = []
        add = out.append
        for x in v:
            n = prim(type(x))
            if n is not None:
                add((n, x))
                continue
            got = memo(id(x))
            add(got if got is not None else fp(x, d))
        return out

    def _dict(self, v, depth):
        prim = _PRIM_NAME.get
        memo = self.memo.get
        fp = self.fp
        d = depth + 1
        pairs = []
        add = pairs.append
        classes = False
        for k, x in v.items():
            tk = type(k)
            n = prim(tk)
            if n is not None:
                a = (n, k)
            else:
                if not classes and isinstance(k, type):
                    classes = True
                a = memo(id(k))
                if a is None:
                    a = fp(k, d)
            n = prim(type(x))
            if n is not None:
                add((a, (n, x)))
                continue
            b = memo(id(x))
            add((a, b if b is not None else fp(x, d)))
        # A dict keyed by classes is often filled from a set of classes, whose order follows
        # memory addresses and so differs from one import to the next: compare without order.
        if classes:
            return ("d", frozenset(pairs))
        return ("d", tuple(pairs))

    def _cell(self, cell, depth):
        try:
            return self.fp(cell.cell_contents, depth + 1)
        except ValueError:
            return ("empty",)

    @staticmethod
    def _state(v, t):
        """(name, value) pairs of an object's own state, or None when it has none we can read."""
        items = []
        d = getattr(v, "__dict__", None)
        if isinstance(d, dict):
            items.extend(d.items())
        for klass in t.__mro__:
            slots = klass.__dict__.get("__slots__")
            if slots:
                if isinstance(slots, str):
                    slots = (slots,)
                for s in slots:
                    if s in ("__dict__", "__weakref__"):
                        continue
                    try:
                        items.append((s, getattr(v, s)))
                    except AttributeError:
                        pass
        if not items and d is None:
            return None
        return sorted(items, key=lambda kv: kv[0])


SKIP_ATTRS = frozenset(("__module__", "__qualname__", "__doc__", "__dict__", "__weakref__", "__firstlineno__",
                        "__static_attributes__", "__annotations__", "__parameters__", "__orig_bases__",
                        "__abstractmethods__", "_abc_impl", "__builtins__", "__cached__", "__file__", "__loader__",
                        "__spec__", "__name__", "__package__", "__path__", "__annotate__"))


# The class trees whose attribute reads the build-time trace records (see classtrace.py).
ROOT_CLASSES = (("sqlglot.tokens", "_TokenizerBase"), ("sqlglot.parser", "Parser"),
                ("sqlglot.generator", "Generator"), ("sqlglot.dialects.dialect", "Dialect"))
# Dialect attributes that hold a tokenizer, parser or generator class. The dialect metaclass
# creates a class with type("Tokenizer", ...) for a dialect without its own tokenizer; such a
# class is not a module global, so it is named after the dialect attribute that holds it.
OWNER_ATTRS = ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class")


def _resolves(cls):
    """True when module.qualname leads to this very class (module and class dicts only)."""
    m = sys.modules.get(cls.__module__)
    if m is None:
        return False
    obj = vars(m)
    found = None
    for part in cls.__qualname__.split("."):
        found = obj.get(part)
        if not isinstance(found, type):
            return False
        obj = found.__dict__
    return found is cls


def root_classes():
    out = []
    for mod, name in ROOT_CLASSES:
        m = sys.modules.get(mod)
        c = vars(m).get(name) if m is not None else None
        if isinstance(c, type):
            out.append(c)
    return out


def tree_classes(roots=None):
    """Every class of the hooked trees, in creation order below each root."""
    out, seen = [], set()
    stack = list(reversed(roots if roots is not None else root_classes()))
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        out.append(c)
        stack.extend(reversed(type.__subclasses__(c)))
    return out


def class_paths():
    """{id(class): (class, module, path)} for the classes of the hooked trees. The path is the
    qualified name, or for a class that the dialect metaclass created, the dialect's name plus
    the attribute that holds it (e.g. "Athena.tokenizer_class")."""
    classes = tree_classes()
    paths = {}
    for c in classes:
        if _resolves(c):
            paths[id(c)] = (c, c.__module__, c.__qualname__)
    owners = sorted((p[1], p[2], p[0]) for p in paths.values() if hasattr(p[0], "tokenizer_class"))
    for mod, path, d in owners:
        for attr in OWNER_ATTRS:
            c = d.__dict__.get(attr)
            if isinstance(c, type) and id(c) not in paths:
                paths[id(c)] = (c, mod, path + "." + attr)
    # The other classes have no stable name. They are the classes that the metaclass makes and
    # then drops (the default of klass.__dict__.get is built even when the dialect has its own
    # Tokenizer), which nothing can reach, and classes made at run time.
    return paths


def code_base():
    """Fingerprinter.base_codes() of the code objects of the functions that the loaded sqlglot
    modules hold (module globals, class attributes, and the functions in the tables (dict
    values) there), for the snapshots of this process and of the processes forked from it. A
    code object that this walk does not reach is hashed by the snapshot as before."""
    f = Fingerprinter()
    code = f.code
    seen = set()
    fn = types.FunctionType
    wrappers = (classmethod, staticmethod)

    def scan(x):
        t = type(x)
        if t is fn:
            code(x.__code__)
        elif t in wrappers:
            x = x.__func__
            if type(x) is fn:
                code(x.__code__)
        elif t is property:
            for g in (x.fget, x.fset, x.fdel):
                if type(g) is fn:
                    code(g.__code__)
        elif t is dict:
            for y in x.values():
                if type(y) is fn:
                    code(y.__code__)
        elif isinstance(x, type):
            if x in seen:
                return
            seen.add(x)
            for y in list(x.__dict__.values()):
                scan(y)

    for mod_name, m in sorted(sys.modules.items()):
        if m is None or not (mod_name == "sqlglot" or mod_name.startswith("sqlglot.")):
            continue
        for v in list(vars(m).values()):
            scan(v)
    return f.base_codes()


def snapshot(code_alias=None, base_codes=None, only=None):
    """{key: fingerprint} for the loaded sqlglot modules.

    Keys: ("G", module, name) for a module global, ("A", module, class, name) for a class attribute,
    ("O", module, class-or-global path, attribute, name) for the attributes of an object that
    such a key holds (one level down, so that a changed setting is named by its own attribute).
    only: the keys of these modules only (cone.py: the other modules are known to be unchanged),
    in the order of a full snapshot."""
    f = Fingerprinter(code_alias, base_codes)
    out = {}
    # the dict, set and frozenset values themselves, by key: keymap.py compares two of them
    # entry by entry when the patch changes one
    f.objects = {}
    seen_classes = set()
    f.function_keys = set()  # keys whose value is a function (found by name at run time, also dynamically)

    prim = _PRIM_NAME.get
    fp = f.fp
    function_types = _FUNCTION_TYPES
    tables = _TABLE_TYPES
    plain = _PLAIN

    def add_value(key, v):
        t = type(v)
        n = prim(t)
        if n is not None:
            out[key] = (n, v)  # (f.fp of a plain value; a builtin type: the steps below do nothing else)
            return
        if t in function_types:
            f.function_keys.add(key)
        if t not in plain:  # (the type of a plain table, a function: its __module__ is not sqlglot's)
            mod = getattr(t, "__module__", "") or ""
            if mod.startswith("sqlglot") and not isinstance(v, (type, enum.Enum)) and t is not types.FunctionType:
                state = f._state(v, t)
                if state:
                    for name, x in state:
                        out[("O",) + key[1:] + (name,)] = fp(x)
                    return
        if t in tables:
            f.objects[key] = v
        out[key] = fp(v)

    def add_class(cls, mod=None, path=None):
        if cls in seen_classes:
            return
        seen_classes.add(cls)
        mod = mod or cls.__module__
        path = path or cls.__qualname__
        for name, v in list(cls.__dict__.items()):
            if name in SKIP_ATTRS:
                continue
            if isinstance(v, type) and v.__module__ == cls.__module__ and v.__qualname__.startswith(cls.__qualname__ + "."):
                add_class(v)
                continue
            add_value(("A", mod, path, name), v)
        out[("A", mod, path, "__bases__")] = f.fp(cls.__bases__)

    def add_module(mod_name, m):
        for name, v in list(vars(m).items()):
            if name in SKIP_ATTRS:
                continue
            if isinstance(v, type):
                if v.__module__ == mod_name:
                    add_class(v)
                else:
                    out[("G", mod_name, name)] = f.fp(v)
                continue
            if isinstance(v, types.ModuleType):
                out[("G", mod_name, name)] = ("M", v.__name__)
                continue
            add_value(("G", mod_name, name), v)

    if _cfp is not None and type(f.fp) is _cfp.FP:
        # the three steps above in C (native/_statefpc.c): the same calls in the same order; a
        # value whose type's metaclass compares or hashes in its own way goes to the Python
        # add_value above
        adder = _cfp.Adder(f, out, seen_classes, f.function_keys, f.objects, SKIP_ATTRS, add_value)
        add_value, add_class, add_module = adder.add_value, adder.add_class, adder.add_module

    for mod_name, m in sorted(sys.modules.items()):
        if m is None or not (mod_name == "sqlglot" or mod_name.startswith("sqlglot.")):
            continue
        if only is not None and mod_name not in only:
            continue
        add_module(mod_name, m)
    # classes of the hooked trees that no module global names (the dialect metaclass makes
    # them): their attributes are compared too, under the name of the dialect attribute
    for c, mod, path in class_paths().values():
        if only is not None and mod not in only:
            continue
        if c not in seen_classes:
            add_class(c, mod, path)
    return out, f


def class_state(cls, mod, path, f):
    """{key: fingerprint} of one class (and the classes nested in it), with the keys and values
    that snapshot() gives for it; f: the Fingerprinter (its objects and function_keys are filled
    as snapshot() fills them)."""
    out = {}
    seen = set()

    def add_value(key, v):
        t = type(v)
        if t in (types.FunctionType, classmethod, staticmethod, property, types.MethodType):
            f.function_keys.add(key)
        tm = getattr(t, "__module__", "") or ""
        if tm.startswith("sqlglot") and not isinstance(v, (type, enum.Enum)) and t is not types.FunctionType:
            state = f._state(v, t)
            if state:
                for name, x in state:
                    out[("O",) + key[1:] + (name,)] = f.fp(x)
                return
        if t is dict or t is set or t is frozenset:
            f.objects[key] = v
        out[key] = f.fp(v)

    def add_class(c, m, p):
        if c in seen:
            return
        seen.add(c)
        for name, v in list(c.__dict__.items()):
            if name in SKIP_ATTRS:
                continue
            if isinstance(v, type) and v.__module__ == c.__module__ and v.__qualname__.startswith(c.__qualname__ + "."):
                add_class(v, v.__module__, v.__qualname__)
                continue
            add_value(("A", m, p, name), v)
        out[("A", m, p, "__bases__")] = f.fp(c.__bases__)

    add_class(cls, mod, path)
    return out


def diff(base, new):
    """Keys whose value differs, or that exist on one side only."""
    changed = [k for k, v in new.items() if base.get(k, _MISSING) != v]
    changed += [k for k in base if k not in new]
    return changed


def diff_only(base, new, only):
    """diff() when new holds the keys of the modules in only (snapshot(only=...)) and the other
    modules are known to be as they were: the same keys, in the same order, as diff() gives."""
    changed = [k for k, v in new.items() if base.get(k, _MISSING) != v]
    changed += [k for k in base if k[1] in only and k not in new]
    return changed


_MISSING = object()
