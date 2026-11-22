"""Builds maps/eqv.txt from recording runs of the pinned suite with tools/eqv/sitecustomize.py.

Record (from a scratch copy of the pinned tree, never the shared one):
  cd <tree> && SKIP_INTEGRATION=0 EQV_OUT=<dir> EQV_ROOT=<tree> PYTHONPATH=<tree>:tools/eqv \
    PATH=<venv>/bin:$PATH python -m pytest tests -p no:cacheprovider -q
usage: build.py <EQV_OUT dir>... <out file>
Keeps the sites that ran and never disagreed or raised in every run given (one per
machine: a site has to be clean on each), one line per file:
<path>\t<line>:<col>:<op> ...
"""

import glob
import sys
from collections import defaultdict


def load(rec):
    tags = defaultdict(set)
    files = glob.glob(rec + "/*.txt")
    for fn in files:
        with open(fn) as f:
            for ln in f:
                tag, key = ln.split()
                tags[key].add(tag)
    bad = sum(1 for t in tags.values() if t != {"R"})
    print(f"{rec}: processes={len(files)} reached={len(tags)} disagreed_or_raised={bad}", file=sys.stderr)
    return {k for k, t in tags.items() if t == {"R"}}


def main():
    *recs, out = sys.argv[1:]
    clean = set.intersection(*(load(r) for r in recs))
    safe = defaultdict(list)
    for key in clean:
        path, site = key.split(":", 1)
        safe[path].append(site)
    with open(out, "w") as f:
        for path in sorted(safe):
            f.write(path + "\t" + " ".join(sorted(safe[path])) + "\n")
    print(f"safe={len(clean)}", file=sys.stderr)


if __name__ == "__main__":
    main()
