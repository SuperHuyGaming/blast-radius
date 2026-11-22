"""Stable names for objects, shared by the map builder and the selector.

A dictionary key or set member that a test looks up is recorded under the name
`knorm` gives it; the selector names the entries a patch changes the same way.
Names only need to be equal for equal objects; two different objects sharing a
name makes a selection wider, never narrower.
"""

import enum

ALL = "*"


def cname(c):
    return f"{c.__module__}:{c.__qualname__}"


def knorm(k, depth=0):
    t = type(k)
    if t is str:
        return "s" + k
    if t is int or t is bool or t is float or k is None:
        return "n" + repr(k)
    if isinstance(k, type):
        return "c" + k.__name__
    if isinstance(k, enum.Enum):
        return "e" + t.__name__ + "." + k.name
    if (t is tuple or t is frozenset) and depth < 3:
        parts = [knorm(x, depth + 1) for x in k]
        if t is frozenset:
            parts.sort()
        return "t(" + ",".join(parts) + ")"
    if t is bytes:
        return "b" + repr(k)
    return "o" + t.__name__


def fingerprint(expression):
    """A digest of an expression tree: its printed form plus its serialized form
    (which carries types and comments). Used to tell whether the optimizer still
    produces, for a query, exactly what it produced at the pinned commit."""
    import hashlib
    import json

    try:
        from sqlglot import serde

        payload = json.dumps(serde.dump(expression), sort_keys=True, default=repr)
    except Exception as e:  # noqa
        payload = "!" + repr(e)
    return hashlib.md5((repr(expression) + "\0" + payload).encode("utf-8", "replace")).hexdigest()
