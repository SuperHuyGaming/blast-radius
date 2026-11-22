"""Turn the raw recording from mapplugin.py into the compact map the selector loads.

    python tools/pack_maps.py RAW.pickle SQLGLOT_CHECKOUT OUT_DIR [TIMING_RAW.pickle]

Everything is keyed by test id, so the selector works with whatever numbering
/data/tests.json uses.  Test sets are stored as Python ints used as bitsets
(bit i = i-th test id in `tests`).
"""

import ast
import collections
import marshal
import os
import pickle
import sys


def main():
    raw_path, repo, out = sys.argv[1:4]
    raw = pickle.load(open(raw_path, "rb"))
    timing = pickle.load(open(sys.argv[4], "rb"))["tests"] if len(sys.argv) > 4 else raw["tests"]
    tests = raw["tests"]
    ids = list(tests)
    idx = {t: i for i, t in enumerate(ids)}
    klass = [t.rsplit("::", 1)[0] for t in ids]
    tfile = [t.split("::", 1)[0] for t in ids]

    # things done once per class (setUpClass) count for every test of the class
    def per_class(field):
        d = collections.defaultdict(set)
        for t, r in tests.items():
            d[t.rsplit("::", 1)[0]].update(map(_tup, r[field]))
        return d

    cs_lines, cs_opens = per_class("setup"), per_class("sopens")
    cs_classes, cs_reads = per_class("sclasses"), per_class("sreads")

    lines = collections.defaultdict(lambda: collections.defaultdict(int))  # file -> line -> bits
    opens = collections.defaultdict(int)
    opens_data = collections.defaultdict(int)
    users = collections.defaultdict(int)
    attr_reads = collections.defaultdict(int)  # "cname\tattr" -> bits
    bag_reads = collections.defaultdict(lambda: collections.defaultdict(int))  # bag -> key -> bits
    # Files a class opens once in setUpClass: when the recording says which class
    # attributes hold their data, only the tests that read those attributes depend
    # on the file; otherwise every test of the class does.
    class_files = raw.get("class_files", {})
    own_reads = collections.defaultdict(set)  # (class key, attribute) -> tests reading it in their own body
    for t, r in tests.items():
        k = t.rsplit("::", 1)[0]
        mod = k.split("::")[0][:-3].replace("/", ".") + ":" + k.split("::")[1]
        for a, b in map(_tup, r["reads"]):
            if a == mod:
                own_reads[(k, b)].add(t)
    narrowed = collections.defaultdict(dict)  # class key -> file -> set of tests
    for k, files in class_files.items():
        for f, attrs in files.items():
            if attrs:
                users_ = set()
                for a in attrs:
                    users_ |= own_reads.get((k, a), set())
                narrowed[k][f] = users_
    for t, r in tests.items():
        bit = 1 << idx[t]
        k = t.rsplit("::", 1)[0]
        for f, ln in set(map(_tup, r["call"])) | set(map(_tup, r["teardown"])) | cs_lines[k]:
            lines[f][ln] |= bit
        for p in set(r["opens"]) | cs_opens[k]:
            opens[p] |= bit
        # the same, counting a setUpClass file only for the tests that use its data;
        # valid as long as setUpClass still loads the file
        for p in set(r["opens"]) | {p for p in cs_opens[k] if p not in narrowed[k] or t in narrowed[k][p]}:
            opens_data[p] |= bit
        for c in set(r["classes"]) | cs_classes[k]:
            users[c] |= bit
        for a, b in set(map(_tup, r["reads"])) | cs_reads[k]:
            if isinstance(a, int):
                bag_reads[a][b] |= bit
            else:
                attr_reads[a + "\t" + b] |= bit
    # fixtures a test module reads while it is being imported count for all its tests
    file_bits = collections.defaultdict(int)
    for i, f in enumerate(tfile):
        file_bits[f] |= 1 << i
    for node, paths in raw["collect_opens"].items():
        if node in file_bits:
            for p in paths:
                if not p.endswith(".py") and not p.startswith("dir:"):
                    opens[p] |= file_bits[node]

    key_reads = {}  # label -> {key: bits}
    for bid, labels in raw["bags"].items():
        kr = dict(bag_reads.get(bid, {}))
        for lab in labels:
            if lab in key_reads:
                for kk, vv in kr.items():
                    key_reads[lab][kk] = key_reads[lab].get(kk, 0) | vv
            else:
                key_reads[lab] = dict(kr)

    # lines that run while a module is imported
    import_lines = collections.defaultdict(dict)
    for (f, ln), mods in raw["import_lines"].items():
        import_lines[f][ln] = tuple(mods)

    # which modules a module needs: what it names in import statements, plus what
    # its namespace actually holds after import (covers lazy re-exports)
    deps = static_imports(repo)

    secs = [round((timing[t] if t in timing else tests[t])["secs"], 4) for t in ids]

    data = dict(
        tests=ids,
        lines={f: dict(d) for f, d in lines.items()},
        import_lines=dict(import_lines),
        opens=dict(opens),
        opens_data={p: v for p, v in opens_data.items() if v != opens.get(p)},
        unit_lists=raw.get("unit_lists", {}),
        unit_fp=raw.get("unit_fp", {}),
        class_files={k: {os.path.normpath(f): a for f, a in v.items() if not f.startswith("dir:")}
                     for k, v in class_files.items()},
        users=dict(users),
        attr_reads=dict(attr_reads),
        key_reads=key_reads,
        class_info=raw["class_info"],
        module_order=raw["module_order"],
        deps=deps,
        secs=secs,
    )
    os.makedirs(out, exist_ok=True)
    # split so the selector only loads what a batch needs
    head = {k: v for k, v in data.items() if k not in ("lines", "key_reads", "attr_reads", "import_lines")}
    head["import_lines"] = {f: frozenset(d) for f, d in import_lines.items()}
    _dump(os.path.join(out, "head.bin"), head)
    _dump(os.path.join(out, "import_lines.bin"), dict(import_lines))
    by_class = collections.defaultdict(dict)
    for k, v in attr_reads.items():
        c, a = k.split("\t", 1)
        by_class[c][a] = v
    rblob = bytearray()
    rindex = {}
    for key, val in [("a" + c, d) for c, d in by_class.items()] + [("k" + l, d) for l, d in key_reads.items()]:
        b = marshal.dumps(val)
        rindex[key] = (len(rblob), len(b))
        rblob += b
    _dump(os.path.join(out, "reads.idx"), rindex)
    with open(os.path.join(out, "reads.bin"), "wb") as fh:
        fh.write(rblob)
    blob = bytearray()
    index = {}
    for f, d in sorted(lines.items()):
        b = marshal.dumps(dict(d))
        index[f] = (len(blob), len(b))
        blob += b
    _dump(os.path.join(out, "lines.idx"), index)
    with open(os.path.join(out, "lines.bin"), "wb") as fh:
        fh.write(blob)
    for n in sorted(os.listdir(out)):
        print(n, os.path.getsize(os.path.join(out, n)))
    print("tests", len(ids), "files", len(lines), "attr pairs", len(attr_reads), "bags", len(key_reads))


def _tup(x):
    return tuple(x) if isinstance(x, list) else x


def _dump(path, obj):
    with open(path, "wb") as f:
        marshal.dump(obj, f)


def static_imports(repo):
    mods = {}
    for top in ("sqlglot", "tests"):
        for dp, dn, fn in os.walk(os.path.join(repo, top)):
            for n in fn:
                if n.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dp, n), repo)
                    name = rel[:-3].replace(os.sep, ".")
                    if name.endswith(".__init__"):
                        name = name[: -len(".__init__")]
                    mods[name] = rel
    deps = {}
    for name, rel in mods.items():
        try:
            tree = ast.parse(open(os.path.join(repo, rel), "rb").read())
        except SyntaxError:
            continue
        pkg = name if rel.endswith("__init__.py") else name.rpartition(".")[0]
        out = set()
        # only imports that run when the module is imported: not those inside
        # functions, and not those under `if TYPE_CHECKING`
        stack = list(tree.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
                stack.extend(node.orelse)
                continue
            if isinstance(node, ast.Import):
                for a in node.names:
                    _add(out, mods, a.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = pkg.split(".")
                    parts = parts[: len(parts) - node.level + 1]
                    base = ".".join(parts + ([base] if base else []))
                _add(out, mods, base)
                for a in node.names:
                    _add(out, mods, base + "." + a.name)
            else:
                stack.extend(ast.iter_child_nodes(node))
        out.discard(name)
        deps[name] = sorted(out)
    return dict(files=mods, deps=deps)


def _add(out, mods, name):
    # importing a.b.c imports a and a.b too
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        n = ".".join(parts[:i])
        if n in mods:
            out.add(n)


if __name__ == "__main__":
    main()
