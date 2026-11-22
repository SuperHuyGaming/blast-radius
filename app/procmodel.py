"""The process model of the selector: no automatic garbage collection, immortal objects at the
fork points, and a memory valve.

1. Automatic collection is off (gc.disable) in every process. The selector's processes are short
   and allocate a lot (plans, imports, test runs); a collection walks the young objects again and
   again and finds little garbage. Explicit gc.collect() calls (the tests' own, the selector's)
   work as before. No test of the pinned tree depends on when an automatic collection runs: the
   two that observe freeing (test_generator_memory, test_cache_does_not_retain_queries) call
   gc.collect() themselves first.
2. Before each fork point (gc.freeze), the live objects are made immortal (PEP 683: reference
   count UINT_MAX on CPython 3.13). Then the children's reference-count writes no longer copy
   the parent's pages. Objects made in a test are not touched (no freeze runs inside a test).
   An immortal object is never freed, so this can only keep objects alive longer, never shorter.
3. The memory valve: with collection off, cyclic garbage stays until the process ends. The
   parent polls each child's memory (selector.memory_over); past a threshold it sends SIGUSR1,
   and the child turns automatic collection back on and collects (valve()). The parent's own
   memory gets the same when the container's memory passes its threshold.

BR_GC_OFF=0 turns all of this off; BR_IMMORTAL=0 keeps 1 and 3 but not 2."""
import gc
import os
import signal
import sys

GC_OFF = os.environ.get("BR_GC_OFF", "1") == "1"
IMMORTAL = os.environ.get("BR_IMMORTAL", "1") == "1"
_IMM = 0xFFFFFFFF
_state = {"installed": False, "mv": None, "calls": 0, "n": 0}


def valve(signum=None, frame=None):
    """Automatic collection on again, and one collection now (the SIGUSR1 handler)."""
    if not gc.isenabled():
        gc.enable()
        gc.collect()


def _immortal_view():
    """A writable view of memory as 32-bit words, when this interpreter keeps the PEP 683
    reference count in the low 32 bits (CPython 3.13, GIL build, 64-bit) and a probe object
    behaves as immortal. Else None (the freeze stays as it is)."""
    if sys.version_info[:2] != (3, 13) or sys.maxsize <= 2 ** 32 or sys.byteorder != "little":
        return None
    try:
        import sysconfig

        if sysconfig.get_config_var("Py_GIL_DISABLED") or sysconfig.get_config_var("Py_DEBUG"):
            return None
        import ctypes

        mv = memoryview((ctypes.c_uint32 * (1 << 45)).from_address(0)).cast("B").cast("I")

        class _Probe:
            pass

        p = _Probe()
        mv[id(p) >> 2] = _IMM
        held = [p] * 8
        del held
        if sys.getrefcount(p) != _IMM:
            return None
        return mv
    except Exception:  # noqa: BLE001
        return None


def _immortalize():
    mv = _state["mv"]
    imm = _IMM
    # collection is off: the objects made since the last freeze are still in generation 0
    # (objects that an explicit collection moved on stay mortal: only a lost saving)
    objs = gc.get_objects() if _state["calls"] == 0 else gc.get_objects(0)
    _state["calls"] += 1
    ft = type(_immortalize)
    ct = type(_immortalize.__code__)
    codes = [o.__code__ for o in objs if type(o) is ft]
    for i in map(id, objs):
        mv[i >> 2] = imm
    n = len(objs)
    del objs
    seen = set()
    while codes:
        c = codes.pop()
        ci = id(c)
        if ci in seen:
            continue
        seen.add(ci)
        mv[ci >> 2] = imm
        n += 1
        for k in c.co_consts:
            if type(k) is ct:
                codes.append(k)
            else:
                mv[id(k) >> 2] = imm
    _state["n"] += n


def install():
    """Idempotent; main.py calls it before the selector is imported, selector.main() again."""
    if _state["installed"] or not GC_OFF:
        return
    _state["installed"] = True
    gc.disable()
    signal.signal(signal.SIGUSR1, valve)
    if IMMORTAL:
        mv = _immortal_view()
        if mv is not None:
            _state["mv"] = mv
            real_freeze = gc.freeze

            def freeze():
                if _state["mv"] is not None and not gc.isenabled():
                    try:
                        _immortalize()
                    except Exception:  # noqa: BLE001  (the objects stay mortal)
                        pass
                real_freeze()

            gc.freeze = freeze
