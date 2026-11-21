"""Fingerprint of what importing sqlglot leaves behind: the value of every module global and of
every class attribute. Two imports (pinned tree, patched tree) are compared key by key; a
patch that changes import-time code can only matter through the keys that differ."""
from __future__ import annotations

import enum
import marshal
import re
import sys
import types

_PRIMITIVE = (str, int, float, bool, bytes, complex, type(None))
_MARSHAL_OK = (dict, list, tuple, set, frozenset)


class Fingerprinter:
    def __init__(self, code_alias=None):
        self.memo = {}
        self.busy = set()
        self.code_memo = {}
        # code objects that count as equal to another one (a function whose body the patch changed)
        self.code_alias = code_alias or {}

    def code(self, c):
        h = self.code_memo.get(id(c))
        if h is None:
            parts = [c.co_code, c.co_names, c.co_varnames, c.co_freevars, c.co_argcount, c.co_flags]
            for k in c.co_consts:
                parts.append(self.code(k) if isinstance(k, types.CodeType) else (type(k).__name__, repr(k)))
            h = hash(tuple(parts))
            h = self.code_alias.get(h, h)
            self.code_memo[id(c)] = h
        return h

    def fp(self, v, depth=0):
        t = type(v)
        if t in _PRIMITIVE:
            return (t.__name__, v)
        key = id(v)
        got = self.memo.get(key)
        if got is not None:
            return got
        if key in self.busy or depth > 12:
            return ("cycle", t.__name__)
        self.busy.add(key)
        try:
            out = self._fp(v, t, depth)
        finally:
            self.busy.discard(key)
        try:
            out = hash(out)
        except TypeError:
            out = hash(repr(out))
        self.memo[key] = out
        return out

    def _fp(self, v, t, depth):
        if t in _MARSHAL_OK:
            try:
                return ("m", t.__name__, marshal.dumps(v))  # plain data: fast path
            except ValueError:
                pass
            if t is dict:
                return self._dict(v, depth)
            if t is tuple and v and all(isinstance(x, type) for x in v):
                # a tuple of classes (isinstance checks) is often made from a set: no order
                return ("tc", frozenset(self.fp(x, depth + 1) for x in v))
            if t in (list, tuple):
                return (t.__name__, tuple(self.fp(x, depth + 1) for x in v))
            return ("s", frozenset(self.fp(x, depth + 1) for x in v))
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

    def _dict(self, v, depth):
        pairs = [(self.fp(k, depth + 1), self.fp(x, depth + 1)) for k, x in v.items()]
        # A dict keyed by classes is often filled from a set of classes, whose order follows
        # memory addresses and so differs from one import to the next: compare without order.
        if any(isinstance(k, type) for k in v):
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


def snapshot(code_alias=None):
    """{key: fingerprint} for the loaded sqlglot modules.

    Keys: ("G", module, name) for a module global, ("A", module, class, name) for a class attribute,
    ("O", module, class-or-global path, attribute, name) for the attributes of an object that
    such a key holds (one level down, so that a changed setting is named by its own attribute)."""
    f = Fingerprinter(code_alias)
    out = {}
    seen_classes = set()
    f.function_keys = set()  # keys whose value is a function (found by name at run time, also dynamically)

    def add_value(key, v):
        t = type(v)
        if t in (types.FunctionType, classmethod, staticmethod, property, types.MethodType):
            f.function_keys.add(key)
        mod = getattr(t, "__module__", "") or ""
        if mod.startswith("sqlglot") and not isinstance(v, (type, enum.Enum)) and t is not types.FunctionType:
            state = f._state(v, t)
            if state:
                for name, x in state:
                    out[("O",) + key[1:] + (name,)] = f.fp(x)
                return
        out[key] = f.fp(v)

    def add_class(cls):
        if cls in seen_classes:
            return
        seen_classes.add(cls)
        for name, v in list(cls.__dict__.items()):
            if name in SKIP_ATTRS:
                continue
            if isinstance(v, type) and v.__module__ == cls.__module__ and v.__qualname__.startswith(cls.__qualname__ + "."):
                add_class(v)
                continue
            add_value(("A", cls.__module__, cls.__qualname__, name), v)
        out[("A", cls.__module__, cls.__qualname__, "__bases__")] = f.fp(cls.__bases__)

    for mod_name, m in sorted(sys.modules.items()):
        if m is None or not (mod_name == "sqlglot" or mod_name.startswith("sqlglot.")):
            continue
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
    return out, f


def diff(base, new):
    """Keys whose value differs, or that exist on one side only."""
    changed = [k for k, v in new.items() if base.get(k, _MISSING) != v]
    changed += [k for k in base if k not in new]
    return changed


_MISSING = object()
