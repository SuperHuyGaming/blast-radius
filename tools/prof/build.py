"""Builds maps/prof.txt from recording runs of the pinned suite with tools/prof/sitecustomize.py.

Record (from a scratch copy of the pinned tree, never the shared one):
  cd <tree> && SKIP_INTEGRATION=0 PROF_OUT=<dir> PROF_ROOT=<tree> PYTHONPATH=<tree>:tools/prof \
    PATH=<venv>/bin:$PATH python -m pytest tests -p no:cacheprovider -p profplug -q -n 8
usage: build.py <cov.bin> <PROF_OUT dir>... <out file>
One recording per machine; a test differs at a token if it differed in any of them. Lines:
  I <ranges>               tests that did not finish their call phase in every run
  <path>\\t<site> <ranges>  one site token: line:col:op, then the tests where its mutant
                           differs (`-` none); sites that differed outside a test are left out
Ranges are test indexes of cov.bin's `ids`, as "a-b" or "a", comma separated. A site is kept
only when its tests are fewer than the tests that run its line (when the line is in cov.bin),
since otherwise line coverage selects no more.
"""

import base64
import glob
import json
import sys
import zlib
from collections import defaultdict


def ranges(idx):
    out = []
    for i in sorted(idx):
        if out and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return ",".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in out) or "-"


def load(rec):
    ran, bad, called = set(), set(), set()
    diff = defaultdict(set)
    files = glob.glob(rec + "/*.txt")
    for fn in files:
        with open(fn) as f:
            for ln in f:
                ln = ln.rstrip("\n")
                tag, rest = ln[0], ln[2:]
                if tag == "R":
                    ran.update(rest.split())
                elif tag == "X":
                    bad.add(rest)
                elif tag == "C":
                    called.add(rest)
                elif tag == "T":
                    ctx, _, keys = rest.partition("\t")
                    diff[ctx].update(keys.split())
    print(f"{rec}: processes={len(files)} sites={len(ran)} outside_tests={len(bad)} tests_called={len(called)}", file=sys.stderr)
    return ran, bad, called, diff


def main():
    cov, *recs, out = sys.argv[1:]
    with open(cov, "rb") as f:
        raw = json.loads(zlib.decompress(f.read()))
    ids = raw["ids"]
    index = {t: i for i, t in enumerate(ids)}
    sets = [int.from_bytes(base64.b64decode(s), "little") for s in raw["sets"]]
    ran, bad, called = set(), set(), None
    tests = defaultdict(set)  # token key -> test indexes
    for rec in recs:
        r, b, c, diff = load(rec)
        ran |= r
        bad |= b
        called = c if called is None else called & c
        for ctx, keys in diff.items():
            if ctx.startswith("scope:"):
                nid = ctx[len("scope:"):]
                who = [i for t, i in index.items() if not nid or t == nid or t.startswith(nid + "::")]
            elif ctx in index:
                who = [index[ctx]]
            else:
                # a ctx no map test stands for: nothing can be said about its keys
                bad |= keys
                continue
            for k in keys:
                tests[k].update(who)
    incomplete = {i for t, i in index.items() if t not in called}

    def line_count(path, line):
        s = raw["files"].get(path, {}).get("lines", {}).get(str(line))
        return bin(sets[s]).count("1") if s is not None else 0

    rows = defaultdict(list)
    kept = dropped = 0
    for k in ran:
        path, line, _ = k.split(":", 2)
        if k in bad:
            dropped += 1
            continue
        who = tests.get(k, set())
        if len(who) >= line_count(path, int(line)) > 0:
            dropped += 1
            continue
        kept += 1
        rows[path].append(k[len(path) + 1:] + " " + ranges(who))
    with open(out, "w") as f:
        f.write("I " + ranges(incomplete) + "\n")
        for path in sorted(rows):
            f.write(path + "\t" + "\t".join(sorted(rows[path])) + "\n")
    print(f"kept={kept} dropped={dropped} incomplete={len(incomplete)}", file=sys.stderr)


if __name__ == "__main__":
    main()
