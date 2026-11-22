"""Offline policy simulator: plan + decide for each labeled patch, emulate verification with
the labels (a run test is found failing iff it is labeled failing), and report simulated
schedule cost, estimated CPU and misses.

Usage: BR_SRC=... BR_INDEX=... python simulate.py <data dir with commits/ tests.json> <answers.json> [verbose]
"""

import json
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "br"))
import main  # noqa: E402

data, answers = sys.argv[1], sys.argv[2]
verbose = len(sys.argv) > 3
idx = pickle.load(open(os.environ["BR_INDEX"], "rb"))
T = json.load(open(os.path.join(data, "tests.json")))["tests"]
num = {t["id"]: t["number"] for t in T}
sec = {t["id"]: t["seconds"] for t in T}
dur = {t["number"]: round(t["seconds"] * 1000) for t in T}
test_ids = idx["tests"]
sec_idx = {i: sec[t] for i, t in enumerate(test_ids) if t in sec}
cost = main.load_cost()
exp_mask = 0
for i, s in sec_idx.items():
    if s >= main.EXPENSIVE:
        exp_mask |= 1 << i
A = json.load(open(answers))["commits"]
inv = {n: i for i, t in enumerate(test_ids) if t in num for n in [num[t]]}
main.KNOWN_IDS.append(list(sec_idx))


def sched(nums):
    ws = main.split(sorted(nums), dur)
    return 0 if not ws else (max(sum(dur[x] for x in w) for w in ws) + 2000 * len(ws)) / 1000


tot_sim = tot_cpu = tot_orc = 0.0
misses = []
rows = []
for cid in sorted(A):
    path = os.path.join(data, "commits", cid + ".patch")
    if not os.path.exists(path):
        continue
    text = open(path, encoding="utf-8", errors="surrogateescape").read()
    p = main.plan_commit(cid, text, idx, None)
    known = 0
    for i in sec_idx:
        known |= 1 << i
    p.run &= known
    p.listed &= known
    main.decide(p, idx, test_ids, sec_idx, exp_mask, cost)
    fail_idx = {inv[n] for n in A[cid]["failing"] if n in inv}
    cpu = main.est_cost(p, idx, test_ids, p.run, cost) if p.run else (main.CHILD_BASE if p.import_check else 0.0)
    found = fail_idx & set(main.bits(p.run))
    if p.stop_early and found:
        reported = found | set(main.bits(p.run)) | set(main.bits(p.maybe_exp))
    else:
        reported = set(found)
        if p.maybe_exp and (found or not p.run):
            reported |= set(main.bits(p.maybe_exp))
    reported |= set(main.bits(p.listed))
    nums = {num[test_ids[i]] for i in reported if test_ids[i] in num}
    sim = sched(nums)
    orc = sched(set(A[cid]["failing"]))
    miss = bool(set(A[cid]["failing"]) - nums)
    if miss:
        misses.append((cid, A[cid]["kind"], sorted(set(A[cid]["failing"]) - nums)[:5]))
        sim += 300
    tot_sim += sim
    tot_cpu += cpu
    tot_orc += orc
    rows.append((sim - orc + cpu, cid, A[cid]["kind"], sim, orc, cpu, " ".join(p.notes)[:80]))

n = len(rows)
print("patches %d  oracle %.1f  simulated %.1f  est.cpu %.1f  total %.1f  (per 40: %.1f)" % (
    n, tot_orc, tot_sim, tot_cpu, tot_sim + tot_cpu, (tot_sim + tot_cpu) * 40 / max(n, 1)))
print("excess over oracle per 40 commits: %.1f" % ((tot_sim + tot_cpu - tot_orc) * 40 / max(n, 1)))
print("misses", len(misses))
for m in misses:
    print("  MISS", m)
if verbose:
    rows.sort(reverse=True)
    for r in rows[: int(os.environ.get("SIM_ROWS", "25"))]:
        print("  %-8s %-14s excess %.2f sim %.2f orc %.2f cpu %.2f %s" % (r[1], r[2], r[0], r[3], r[4], r[5], r[6]))
