"""Merge per-file coverage outputs into one map keyed by test number.

map.json: {
  "tests": [test ids in tests.json order],
  "lines": {file: {line: hexbitset}},        # runtime coverage (incl. class setup)
  "import_lines": {file: [lines]},            # executed in context "" (import time)
  "reads": {path: hexbitset},                 # files read during tests
  "dialects": {dialect_class: hexbitset},
}
"""

import glob
import json
import sys
from collections import defaultdict

OUT = sys.argv[1]
TESTS = sys.argv[2]
DEST = sys.argv[3]

tests = json.load(open(TESTS))["tests"]
num = {t["id"]: t["number"] for t in tests}

ctxs = {}
for f in glob.glob(OUT + "/*.json"):
    ctxs.update(json.load(open(f)) if False else {})
    d = json.load(open(f))
    for k, v in d.items():
        if k == "":
            ctxs.setdefault("", []).append(v)
        else:
            ctxs[k] = v

# class-level setup: union of all S| contexts of a class goes to all tests in that class
cls_setup = defaultdict(list)
for k, v in ctxs.items():
    if k.startswith("S|"):
        nid = k[2:]
        cls = nid.rsplit("::", 1)[0]
        cls_setup[cls].append(v)

lines = defaultdict(lambda: defaultdict(int))
reads = defaultdict(int)
dialects = defaultdict(int)


def add(v, bit):
    for f, ls in v.get("lines", {}).items():
        d = lines[f]
        for l in ls:
            d[l] |= bit
    for p in v.get("fixtures", []) or []:
        reads[p] |= bit
    for p in v.get("dialects", []) or []:
        dialects[p] |= bit


missing = []
for tid, n in num.items():
    bit = 1 << n
    v = ctxs.get(tid)
    if v is None:
        missing.append(tid)
        continue
    add(v, bit)
    cls = tid.rsplit("::", 1)[0]
    for sv in cls_setup.get(cls, []):
        add(sv, bit)

imp = defaultdict(set)
for v in ctxs.get("", []):
    for f, ls in v["lines"].items():
        imp[f].update(ls)

print("missing tests:", missing, file=sys.stderr)
json.dump(
    {
        "tests": [t["id"] for t in tests],
        "lines": {f: {str(l): format(b, "x") for l, b in d.items()} for f, d in lines.items()},
        "import_lines": {f: sorted(s) for f, s in imp.items()},
        "reads": {p: format(b, "x") for p, b in reads.items()},
        "dialects": {p: format(b, "x") for p, b in dialects.items()},
        "missing": missing,
    },
    open(DEST, "w"),
)
