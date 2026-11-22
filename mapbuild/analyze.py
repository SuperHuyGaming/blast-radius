"""Static recall check: are failing tests covered by the changed lines of each patch?"""

import json
import re
import sys
from pathlib import Path

m = json.load(open(sys.argv[1]))
data = Path(sys.argv[2])  # dir with commits/ and tests.json
ans = json.load(open(sys.argv[3]))["commits"]
tests = json.load(open(data / "tests.json"))["tests"]
secs = {t["number"]: t["seconds"] for t in tests}
lines = {f: {int(l): int(b, 16) for l, b in d.items()} for f, d in m["lines"].items()}
imp = {f: set(v) for f, v in m["import_lines"].items()}
reads = {p: int(b, 16) for p, b in m["reads"].items()}


def parse_patch(text):
    files = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            cur = p[2:] if p.startswith("b/") else p
            files[cur] = []
            continue
        mm = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if mm:
            old = int(mm.group(1))
            continue
        if cur is None or line.startswith("diff ") or line.startswith("index "):
            continue
        if line.startswith("-"):
            files[cur].append(("-", old, line[1:]))
            old += 1
        elif line.startswith("+"):
            files[cur].append(("+", old, line[1:]))  # inserted before old line `old`
        else:
            old += 1
    return files


def bits(b):
    out = []
    while b:
        low = b & -b
        out.append(low.bit_length() - 1)
        b ^= low
    return out


tot_miss = 0
for p in sorted((data / "commits").glob("*.patch")):
    cid = p.stem
    fails = set(ans[cid]["failing"])
    files = parse_patch(p.read_text())
    cand = 0
    notes = []
    for f, ch in files.items():
        if f.startswith("sqlglot/") and f.endswith(".py"):
            d = lines.get(f, {})
            for sign, ln, txt in ch:
                if sign == "-":
                    targets = [ln]
                else:
                    targets = [ln - 1, ln]
                for t in targets:
                    if t in d:
                        cand |= d[t]
                    if t in imp.get(f, ()) and t not in d:
                        notes.append(f"import-only {f}:{t}")
        elif f.startswith("tests/"):
            if f in reads:
                cand |= reads[f]
            else:
                notes.append(f"unread {f}")
    cs = set(bits(cand))
    miss = fails - cs
    tot_miss += bool(miss)
    print(
        cid, ans[cid]["kind"], "fail", len(fails), "cand", len(cs),
        "cand_s %.1f" % sum(secs[x] for x in cs), "MISS" if miss else "", sorted(miss)[:5], notes[:3],
    )
print("commits with misses", tot_miss)
