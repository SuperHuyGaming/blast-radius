"""Merge unit digests taken under several hash seeds into maps/head.bin.

    python tools/pack_digests.py MAPS_DIR DIGESTS.bin [DIGESTS.bin ...]

A digest is kept only when all runs agree on it; one that is not is stored as
"?" and never used for a comparison.
"""
import marshal
import os
import sys


runs = [marshal.load(open(p, "rb")) for p in sys.argv[2:]]
merged = {}
for t in runs[0]:
    n = len(runs[0][t])
    merged[t] = rows = []
    unstable = 0
    for i in range(n):
        ref = runs[0][t][i]
        if not all(len(r.get(t, [])) == n and len(r[t][i]) == len(ref) for r in runs):
            rows.append(None)
            unstable += 1
            continue
        # a digest that is not the same in every run is never used ("?" equals nothing)
        row = [
            v if not isinstance(v, str) or all(r[t][i][k] == v for r in runs) else "?"
            for k, v in enumerate(ref)
        ]
        unstable += "?" in row
        rows.append(row)
    print(t.split("::")[-1], n, "items,", unstable, "with an unstable digest")
path = os.path.join(sys.argv[1], "head.bin")
head = marshal.load(open(path, "rb"))
head["unit_fp"] = merged
marshal.dump(head, open(path, "wb"))
