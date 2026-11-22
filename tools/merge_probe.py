"""Merge probe recordings (several runs, several xdist workers) into maps/probe.bin.

    python tools/merge_probe.py HEAD.bin OUT.bin PROBE_DIR [PROBE_DIR...]

Result: {(file, line, column): (flags, tests)} where flags has bit 1 when the
comparison ran at all and bit 2 when its sibling disagreed outside any test
(at import time), and tests is the set of tests (as a bitset over the map's
test order) during which the sibling disagreed.
"""
import glob, marshal, sys

head = marshal.load(open(sys.argv[1], "rb"))
tests = head["tests"]
bit = {t: 1 << i for i, t in enumerate(tests)}
class_bits = {}
for t, b in bit.items():
    k = t.rsplit("::", 1)[0]
    class_bits[k] = class_bits.get(k, 0) | b
flags, where = {}, {}
files = 0
for d in sys.argv[3:]:
    for f in glob.glob(d + "/probe_gw*.bin") + glob.glob(d + "/probe_main.bin"):
        r = marshal.load(open(f, "rb"))
        if not r["flags"]:
            continue
        files += 1
        for k, v in r["flags"].items():
            flags[k] = flags.get(k, 0) | v
        for t, keys in r["by_test"].items():
            for k in keys:
                where[k] = where.get(k, 0) | bit.get(t, 0)
                if t not in bit:
                    flags[k] = flags.get(k, 0) | 2  # a test the map does not know: no claim
        for c, keys in r["by_class"].items():
            for k in keys:
                where[k] = where.get(k, 0) | class_bits.get(c, 0)
                if c not in class_bits:
                    flags[k] = flags.get(k, 0) | 2
out = {k: (v, where.get(k, 0)) for k, v in flags.items()}
marshal.dump(out, open(sys.argv[2], "wb"))
n_exec = sum(1 for v, _ in out.values() if v & 1)
n_never = sum(1 for v, w in out.values() if v & 1 and not v & 2 and not w)
n_some = sum(1 for v, w in out.values() if not v & 2 and w)
n_imp = sum(1 for v, _ in out.values() if v & 2)
print(f"{files} recordings, {len(out)} comparisons: {n_exec} executed, {n_never} never disagree, "
      f"{n_some} disagree in some tests only, {n_imp} disagree at import time")
import statistics
sizes = sorted(bin(w).count("1") for v, w in out.values() if not v & 2 and w)
if sizes:
    print("tests per disagreeing comparison: median", statistics.median(sizes), "max", sizes[-1], "<=10:", sum(1 for s in sizes if s <= 10))
