"""Checks the recorder against real mutants: random BoolOps and comparisons are written into a
throwaway sqlglot/ package, run under the recorder, then each one-token mutant is run as plain
Python. Wherever the recorder kept a site quiet, the mutant must give the same result, raise the
same way and make the same calls. Prints the counts; exits 1 on an unsound key.

usage: selftest.py [rounds]
"""

import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

VALUES = ["0", "1", "2", "''", "'a'", "None", "[]", "[0]", "True", "False"]


def gen_operand(rng):
    r = rng.random()
    if r < 0.4:
        return "v%d" % rng.randrange(4)
    if r < 0.6:
        return "f(%d, %s)" % (rng.randrange(100), rng.choice(VALUES))
    if r < 0.7:
        return "boom()"
    if r < 0.85:
        return "(v%d %s %s)" % (rng.randrange(4), rng.choice(["<", "<=", ">", ">=", "==", "!=", "is", "is not"]), rng.choice(["0", "1", "2", "None", "v0"]))
    return "(%s %s %s)" % (gen_operand(rng), rng.choice(["and", "or"]), gen_operand(rng))


def gen_expr(rng):
    n = rng.randint(2, 4)
    parts = [gen_operand(rng)]
    for _ in range(n - 1):
        parts.append(rng.choice(["and", "or"]))
        parts.append(gen_operand(rng))
    return parts


MODULE = """
LOG = []
def f(i, v):
    LOG.append(i)
    return v
def boom():
    LOG.append("boom")
    raise ValueError
def run(v0, v1, v2, v3):
    LOG.clear()
    try:
        if %s:
            r = "yes"
        else:
            r = "no"
    except Exception as ex:
        r = "raised " + type(ex).__name__
    try:
        w = %s
    except Exception as ex:
        w = "raised " + type(ex).__name__
    return r, w, list(LOG)
"""


def flips(parts):
    """Source of each mutant: one and/or token outside parentheses flipped."""
    out = []
    for i in range(1, len(parts), 2):
        p = list(parts)
        p[i] = "or" if p[i] == "and" else "and"
        out.append((i, " ".join(p)))
    return out


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    rng = random.Random(7)
    checked = quiet = bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        pkg = os.path.join(tmp, "sqlglot")
        os.makedirs(pkg)
        open(os.path.join(pkg, "__init__.py"), "w").close()
        for rnd in range(rounds):
            parts = gen_expr(rng)
            src = " ".join(parts)
            # the same expression in a truth-tested place (line 13) and as a value (line 19)
            with open(os.path.join(pkg, "m.py"), "w") as f:
                f.write(MODULE % (src, src))
            envs = [[rng.choice(VALUES) for _ in range(4)] for _ in range(6)]
            out = os.path.join(tmp, "rec%d" % rnd)
            os.makedirs(out)
            driver = "import sqlglot.m as m\nfor e in %s:\n    m.run(*[eval(x) for x in e])\n" % envs
            env = dict(os.environ, PROF_OUT=out, PROF_ROOT=tmp, PROF_CTX="t", PYTHONPATH=tmp + os.pathsep + HERE)
            subprocess.run([sys.executable, "-c", driver], env=env, check=True, cwd=tmp)
            differed = set()
            for fn in os.listdir(out):
                for ln in open(os.path.join(out, fn)):
                    if ln.startswith("T "):
                        differed.update(ln.rstrip("\n").split("\t")[1].split())
                    elif ln.startswith("X "):
                        differed.add(ln.split()[1])
            # each top-level token is keyed by the BoolOp node that holds it
            for line, base, ia in ((12, 11, 0), (19, 12, 1)):
                for ti, msrc in flips(parts):
                    key = token_key(src, parts, ti, line, base)
                    checked += 1
                    if key in differed:
                        continue
                    quiet += 1
                    g1 = load(MODULE % (src, src))
                    g2 = load(MODULE % (msrc, msrc))
                    for e in envs:
                        vals = [eval(x) for x in e]
                        ra = g1["run"](*vals)
                        rb = g2["run"](*vals)
                        if canon(ra[ia], vals) != canon(rb[ia], vals) or ra[2] != rb[2]:
                            bad += 1
                            print("UNSOUND", key, repr(src), "->", repr(msrc), e, ra, rb)
                            break
    print(f"tokens checked={checked} kept quiet={quiet} unsound={bad}")
    sys.exit(1 if bad else 0)


def token_key(src, parts, ti, line, base):
    """Key of the BoolOp node whose own operator is token ti of `parts`."""
    import ast

    tok_col = sum(len(p) + 1 for p in parts[:ti])
    for node in ast.walk(ast.parse(src, mode="eval")):
        if isinstance(node, ast.BoolOp):
            for k in range(1, len(node.values)):
                if node.values[k - 1].end_col_offset <= tok_col < node.values[k].col_offset:
                    op = "and" if isinstance(node.op, ast.And) else "or"
                    return "sqlglot/m.py:%d:%d:%s%d" % (line, node.col_offset + base, op, k)
    raise AssertionError(src)


def load(src):
    g = {}
    exec(compile(src, "<m>", "exec"), g)
    return g


def canon(v, vals):
    """A result as the argument it is, or as its value."""
    for i, x in enumerate(vals):
        if v is x:
            return ("arg", i)
    return (type(v).__name__, repr(v))


if __name__ == "__main__":
    main()
