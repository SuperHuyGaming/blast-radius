"""Writes one-operator mutants at profiled sites, to label by running the suite and check that
narrowing to the profile keeps every failing test.

usage: synth.py <sqlglot tree> <prof.txt> <cov.bin> <out dir> <count> [seed] [op,...] [id prefix]
Picks sites whose line many tests run, half where the profile names a few tests and half where
it names none, over and/or tokens, < <=, > >=, is ==. Writes <out>/commits/<id>.patch.
"""

import ast
import difflib
import json
import os
import random
import sys
import zlib
import base64

FLIP = {"lt": "<=", "le": "<", "gt": ">=", "ge": ">", "is": "==", "eq": "is", "isnot": "!=", "ne": "is not"}
TEXT = {"lt": "<", "le": "<=", "gt": ">", "ge": ">=", "is": "is", "eq": "==", "isnot": "is not", "ne": "!="}


def offset(lines, line, col):
    """Character offset of (1-based line, utf-8 byte col) in the joined text."""
    pre = sum(len(x) for x in lines[: line - 1])
    return pre + len(lines[line - 1].encode()[:col].decode())


def mutate(src, site):
    line, col, op = site.split(":")
    line = int(line)
    col = int(col)
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != line or node.col_offset != col:
            continue
        if isinstance(node, ast.BoolOp) and op.rstrip("0123456789") in ("and", "or"):
            k = int(op.lstrip("andor"))
            word = "and" if isinstance(node.op, ast.And) else "or"
            if op.rstrip("0123456789") != word:
                continue
            a = node.values[k - 1]
            b = node.values[k]
            lo = offset(lines, a.end_lineno, a.end_col_offset)
            hi = offset(lines, b.lineno, b.col_offset)
            gap = src[lo:hi]
            i = gap.find(word)
            if i < 0 or "#" in gap[:i]:
                return None
            other = "or" if word == "and" else "and"
            return src[: lo + i] + other + src[lo + i + len(word) :]
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and op in FLIP:
            a = node.left
            b = node.comparators[0]
            lo = offset(lines, a.end_lineno, a.end_col_offset)
            hi = offset(lines, b.lineno, b.col_offset)
            gap = src[lo:hi]
            t = TEXT[op]
            i = gap.find(t)
            if i < 0 or (op == "is" and gap[i:].startswith("is not")) or (op == "lt" and gap[i:].startswith("<=")) or (op == "gt" and gap[i:].startswith(">=")):
                return None
            return src[: lo + i] + FLIP[op] + src[lo + i + len(t) :]
    return None


def main():
    tree, prof, cov, out, count = sys.argv[1:6]
    rng = random.Random(int(sys.argv[6]) if len(sys.argv) > 6 else 11)
    kinds = set(sys.argv[7].split(",")) if len(sys.argv) > 7 else None
    prefix = sys.argv[8] if len(sys.argv) > 8 else "p"
    with open(cov, "rb") as f:
        raw = json.loads(zlib.decompress(f.read()))
    sets = [int.from_bytes(base64.b64decode(s), "little") for s in raw["sets"]]
    few, none = [], []
    for ln in open(prof):
        if ln.startswith("I "):
            continue
        path, *items = ln.rstrip("\n").split("\t")
        lines = raw["files"].get(path, {}).get("lines", {})
        for it in items:
            site, r = it.split(" ")
            if kinds and site.split(":")[2].rstrip("0123456789") not in kinds:
                continue
            s = lines.get(site.split(":")[0])
            n = bin(sets[s]).count("1") if s is not None else 0
            if n < 100:
                continue
            ntests = 0 if r == "-" else sum(1 + (int(b) - int(a) if b else 0) for a, _, b in (x.partition("-") for x in r.split(",")))
            (none if ntests == 0 else few).append((path, site, ntests, n))
    few = [x for x in few if x[2] <= 40]
    rng.shuffle(few)
    rng.shuffle(none)
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    made = []
    for pool, want in ((few, int(count) - int(count) // 3), (none, int(count) // 3)):
        got = 0
        for path, site, ntests, n in pool:
            if got >= want:
                break
            src = open(os.path.join(tree, path)).read()
            new = mutate(src, site)
            if new is None or new == src:
                continue
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            diff = difflib.unified_diff(src.splitlines(keepends=True), new.splitlines(keepends=True), "a/" + path, "b/" + path)
            cid = prefix + "%02d%s" % (len(made), site.split(":")[2])
            with open(os.path.join(out, "commits", cid + ".patch"), "w") as f:
                f.write("diff --git a/%s b/%s\n" % (path, path))
                f.writelines(diff)
            made.append((cid, path, site, ntests, n))
            got += 1
    for m in made:
        print(*m)


if __name__ == "__main__":
    main()
