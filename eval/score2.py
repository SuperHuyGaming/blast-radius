"""Per-commit breakdown: selected vs failing, misses, cost, oracle cost."""
import json, sys, collections
data, out = sys.argv[1], sys.argv[2]
T = {t["number"]: t["seconds"] for t in json.load(open(data + "/tests.json"))["tests"]}
A = json.load(open(data + "/answers.json"))["commits"]
ms = {k: round(v * 1000) for k, v in T.items()}
def cost(workers):
    w = [x for x in workers if x]
    return (max(sum(ms[t] for t in g) for g in w) + 2000 * len(w)) / 1000 if w else 0
tot = orc = 0; miss = 0; rows = []
by = collections.defaultdict(lambda: [0, 0, 0, 0])
for line in open(out):
    o = json.loads(line); a = A[o["commit"]]
    sel = {t for g in o["workers"] for t in g}; f = set(a["failing"])
    c = cost(o["workers"]); m = bool(f - sel)
    oc = (sum(ms[t] for t in f) / 1000 + 2) if f else 0
    tot += c + 300 * m; orc += oc; miss += m
    k = a.get("subkind", a.get("kind"))
    by[k][0] += 1; by[k][1] += c + 300 * m; by[k][2] += oc; by[k][3] += m
    rows.append((c + 300 * m - oc, o["commit"], k, len(sel), len(f), m, a.get("desc", "")))
print(f"total {tot:.1f}  oracle {orc:.1f}  misses {miss}  n={len(rows)}")
for k, v in sorted(by.items(), key=lambda x: -(x[1][1] - x[1][2])):
    print(f"  {k:28s} n={v[0]:3d} cost={v[1]:7.1f} oracle={v[2]:7.1f} miss={v[3]}")
print("worst:")
for r in sorted(rows, reverse=True)[:int(sys.argv[3]) if len(sys.argv) > 3 else 12]:
    print("  %.1f %s %s sel=%d fail=%d miss=%s %s" % r)
