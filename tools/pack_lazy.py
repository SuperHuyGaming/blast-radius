"""Lines that some test executes only when dialects load lazily (a plain pytest
run) and not in the preloaded recording run.

    python tools/pack_lazy.py PRELOADED_RAW.pickle LAZY_RAW.pickle OUT_DIR

Which test first loads a dialect depends on test order and process layout, so
the recorded line -> tests map cannot be trusted for these lines; the selector
runs everything when a patch touches a function that contains one.
"""
import collections, marshal, os, pickle, sys


def cov(raw):
    out = collections.defaultdict(set)
    cs = collections.defaultdict(set)
    for t, r in raw["tests"].items():
        cs[t.rsplit("::", 1)[0]] |= set(map(tuple, r["setup"]))
    for t, r in raw["tests"].items():
        for fl in set(map(tuple, r["call"])) | set(map(tuple, r["teardown"])) | cs[t.rsplit("::", 1)[0]]:
            out[fl].add(t)
    return out


pre = cov(pickle.load(open(sys.argv[1], "rb")))
lazy = cov(pickle.load(open(sys.argv[2], "rb")))
out = collections.defaultdict(list)
for (f, ln), ts in lazy.items():
    if f.startswith("sqlglot/") and ts - pre.get((f, ln), set()):
        out[f].append(ln)
out = {f: sorted(v) for f, v in out.items()}
marshal.dump(out, open(os.path.join(sys.argv[3], "lazy.bin"), "wb"))
print(out)
