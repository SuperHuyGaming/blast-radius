"""Run the selector natively on a dataset and show per-commit cost and misses.

usage: eval.py <dataset dir with commits/ answers.json [tests.json]> [--src <sqlglot>] [--maps <dir>]
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--src", default=os.path.expanduser("~/ws/buildathon/work/sqlglot"))
    ap.add_argument("--maps", default=os.path.join(HERE, "maps"))
    ap.add_argument("--tests", default=os.path.join(HERE, "sample", "tests.json"))
    ap.add_argument("-q", action="store_true")
    a = ap.parse_args()
    data = os.path.abspath(a.data)
    tdir = data
    if not os.path.exists(os.path.join(data, "tests.json")):
        tdir = os.path.dirname(a.tests)
    env = dict(os.environ, BLAST_DATA=data, BLAST_SRC=a.src, BLAST_MAPS=a.maps, BLAST_VERBOSE="1")
    if tdir != data:
        # selector reads tests.json from BLAST_DATA; give it a view
        view = os.path.join("/tmp", f"blast-eval-{os.getpid()}")
        os.makedirs(view, exist_ok=True)
        for name, src in (("commits", os.path.join(data, "commits")), ("tests.json", a.tests)):
            p = os.path.join(view, name)
            if not os.path.exists(p):
                os.symlink(src, p)
        env["BLAST_DATA"] = view
    t0 = time.time()
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    p = subprocess.run([sys.executable, os.path.join(HERE, "selector.py")], env=env, capture_output=True, text=True)
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    if p.returncode:
        print(p.stderr)
        sys.exit(1)
    notes = {}
    for line in p.stderr.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            notes[k] = v
    tests = json.load(open(a.tests))["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ans = json.load(open(os.path.join(data, "answers.json")))["commits"]
    total = 0
    misses = 0
    rows = []
    for line in p.stdout.splitlines():
        o = json.loads(line)
        ws = [w for w in o["workers"] if w]
        cost = (max(sum(ms[n] for n in w) for w in ws) + 2000 * len(ws)) if ws else 0
        sel = {n for w in ws for n in w}
        fail = set(ans[o["commit"]]["failing"])
        miss = bool(fail - sel)
        oracle = 0
        if fail:
            oracle = max(ms[n] for n in fail) + 2000
        total += cost + (300000 if miss else 0)
        misses += miss
        rows.append((o["commit"], ans[o["commit"]].get("kind"), len(sel), cost / 1000, oracle / 1000, miss, sorted(fail - sel)[:5], notes.get(o["commit"], "")))
    rows.sort(key=lambda r: -(r[3] + 300 * r[5]))
    if not a.q:
        for r in rows:
            print(f"{r[0]} {r[1]:8} n={r[2]:5} cost={r[3]:7.3f} oracle~{r[4]:6.3f} {'MISS ' + str(r[6]) if r[5] else ''}  {r[7][:160]}")
            for key in ("tpcds: ", "suite: "):
                if key in r[7][160:]:
                    print("    " + next(x for x in r[7].split(" | ") if x.startswith(key)))
    for key in ("tpcds runs", "suite runs"):
        if key in notes:
            print(f"{key}: {notes[key]}")
    print(f"TOTAL simulated={total / 1000:.1f}s misses={misses} commits={len(rows)} selector_cpu={cpu:.2f}s wall={time.time() - t0:.2f}s")


if __name__ == "__main__":
    main()
