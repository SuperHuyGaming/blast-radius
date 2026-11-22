"""Excess over the best possible packing, per commit and per kind."""
import json, sys, collections
data, out = sys.argv[1], sys.argv[2]
T = {t["number"]: t for t in json.load(open(data + "/tests.json"))["tests"]}
A = json.load(open(data + "/answers.json"))["commits"]
ms = {k: round(v["seconds"] * 1000) for k, v in T.items()}
def cost(workers):
    w = [x for x in workers if x]
    return (max(sum(ms[t] for t in g) for g in w) + 2000 * len(w)) / 1000 if w else 0
def pack_cost(tests):
    if not tests: return 0.0
    order = sorted(tests, key=lambda t: -ms[t]); best = None
    for k in range(1, min(16, len(order)) + 1):
        loads = [0] * k
        for t in order:
            i = loads.index(min(loads)); loads[i] += ms[t]
        c = max(loads) + 2000 * k
        if best is None or c < best: best = c
        if max(loads) <= ms[order[0]]: break
    return best / 1000
tot = orc = 0; rows = []; pure = part = 0.0; npure = npart = 0
by = collections.defaultdict(lambda: [0, 0.0, 0.0, 0])
for line in open(out):
    o = json.loads(line); a = A[o["commit"]]
    sel = {t for g in o["workers"] for t in g}; f = set(a["failing"])
    c = cost(o["workers"]); oc = pack_cost(f); m = bool(f - sel)
    tot += c; orc += oc
    ex = c - oc
    if ex > 0.01:
        if not f: pure += ex; npure += 1
        else: part += ex; npart += 1
    k = a.get("subkind", a.get("kind"))
    by[k][0] += 1; by[k][1] += c; by[k][2] += oc; by[k][3] += ex > 0.01
    rows.append((ex, o["commit"], k, len(sel), len(f), sorted(sel - f, key=lambda t: -ms[t])[:3], a.get("desc", "")))
n = len(rows)
print(f"n={n} total {tot:.1f} oracle {orc:.1f} excess {tot-orc:.1f} ({(tot-orc)/n*30:.1f} per 30) | nothing fails: {npure} commits {pure:.1f}s | some fail: {npart} commits {part:.1f}s")
for k, v in sorted(by.items(), key=lambda x: -(x[1][1] - x[1][2])):
    print(f"  {k:28s} n={v[0]:3d} cost={v[1]:7.1f} oracle={v[2]:7.1f} excess={v[1]-v[2]:6.1f} commits-with-excess={v[3]}")
N = int(sys.argv[3]) if len(sys.argv) > 3 else 15
for r in sorted(rows, key=lambda r: -r[0])[:N]:
    print("  %.1f %s %s sel=%d fail=%d %s %s" % (r[0], r[1], r[2], r[3], r[4], [(T[t]["id"].split("::")[-1], ms[t]) for t in r[5]], r[6][:60]))
