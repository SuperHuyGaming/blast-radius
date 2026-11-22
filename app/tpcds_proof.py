"""Leave test_execute_tpcds out of a commit when the patch provably cannot change its outcome.

The test parses the TPC-DS fixture queries, pickles them to a pool, runs execute() (schema,
optimize, plan, Python executor) and compares each result with DuckDB running
transpile(sql, write="duckdb"). Split it at the three digested calls (parse, the optimize()
inside execute(), the DuckDB transpile): every other sqlglot function the test starts is
"downstream", recorded per code object from the pinned tree in maps/tpcds_d.json.

The test is left out only when
1. the patch only modifies existing .py files under sqlglot/, every hunk applies exactly;
2. every changed line lies in the body of a function whose code object never starts
   downstream, so all downstream code is unchanged; and
3. a child running the patched tree gives the same per-query digest of the parsed trees,
   optimized trees and DuckDB SQL as the pinned tree (tpcds_digest.py, fixed hash seed).
Then the downstream code and its inputs are the same, so its verdict is the pinned one: pass.
"""

import ast
import difflib
import json
import os
import re
import subprocess
import sys
import time

TID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
HERE = os.path.dirname(os.path.abspath(__file__))
MIN_GAIN = 6.0  # seconds of answer a check must be able to save


def parse_patch(text):
    files, cur, hunk = [], None, None
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "meta": []}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if raw.startswith("--- ") and hunk is None:
            p = raw[4:].strip()
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            continue
        if raw.startswith("+++ ") and hunk is None:
            p = raw[4:].strip()
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
        if m:
            hunk = [int(m.group(1)), []]
            cur["hunks"].append(hunk)
            continue
        if hunk is not None and raw[:1] in (" ", "-", "+"):
            hunk[1].append((raw[0], raw[1:]))
        elif raw.startswith(("new file", "deleted file", "similarity", "rename ", "old mode", "new mode", "Binary")):
            cur["meta"].append(raw)
    return files


def apply_exact(old_text, hunks):
    """Apply hunks; None unless every context and removed line matches the pinned file exactly."""
    old = old_text.split("\n")
    out, pos = [], 0
    for start, lines in hunks:
        s = start - 1 if any(t != "+" for t, _ in lines) else start
        if s < pos:
            return None
        out.extend(old[pos:s])
        pos = s
        for tag, text in lines:
            if tag in (" ", "-"):
                if pos >= len(old) or old[pos] != text:
                    return None
                if tag == " ":
                    out.append(text)
                pos += 1
            else:
                out.append(text)
    out.extend(old[pos:])
    return "\n".join(out)


def changed_old_lines(old, new):
    a, b = old.splitlines(), new.splitlines()
    lines = set()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            lines.update(range(i1 + 1, i2 + 1))
        else:
            lines.update((i1, i1 + 1))  # pure insertion: both neighbours
    return lines


def body_owner(tree, line):
    """First line (decorators included) of the innermost function whose body holds line, else None."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lo = node.body[0].lineno
            hi = max(getattr(n, "end_lineno", n.lineno) for n in node.body)
            first = min([d.lineno for d in node.decorator_list] + [node.lineno])
        elif isinstance(node, ast.Lambda):
            lo, hi, first = node.body.lineno, node.body.end_lineno, node.lineno
        else:
            continue
        if lo <= line <= hi and (best is None or lo >= best[0]):
            best = (lo, first)
    return None if best is None else best[1]


def eligible(root, patch_text, downstream):
    """{path: new text} when the static rule allows a digest check, else None."""
    files = parse_patch(patch_text)
    if not files:
        return None
    out = {}
    for f in files:
        path = f["new"]
        if f["meta"] or not f["old"] or f["old"] != path or not path.startswith("sqlglot/") or not path.endswith(".py"):
            return None
        try:
            with open(os.path.join(root, path), encoding="utf-8") as fh:
                old = fh.read()
        except OSError:
            return None
        new = apply_exact(old, f["hunks"])
        if new is None:
            return None
        if new == old:
            continue
        try:
            tree = ast.parse(old)
            ast.parse(new)
        except SyntaxError:
            return None
        d = set(downstream.get(path, ()))
        for line in changed_old_lines(old, new):
            first = body_owner(tree, line)
            if first is None or first in d:
                return None
        out[path] = new
    return out or None


def digest_equal(root, files, baseline, timeout):
    tmp = os.path.join("/tmp", "tpcds_%d" % os.getpid())
    os.makedirs(tmp, exist_ok=True)
    overlay = {}
    for i, (path, text) in enumerate(sorted(files.items())):
        p = os.path.join(tmp, "%d.py" % i)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        overlay[path] = p
    ov = os.path.join(tmp, "overlay.json")
    with open(ov, "w") as fh:
        json.dump(overlay, fh)
    try:
        env = dict(os.environ, PYTHONHASHSEED="1", PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, os.path.join(HERE, "tpcds_digest.py"), root, baseline, ov],
                           env=env, capture_output=True, text=True, timeout=timeout, cwd="/tmp")
        return p.returncode == 0 and p.stdout.strip() == "EQUAL"
    except Exception:  # noqa: BLE001
        return False
    finally:
        for name in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def _cost(workers, seconds):
    w = [x for x in workers if x]
    if not w:
        return 0.0
    return max(sum(round(seconds[n] * 1000) for n in x) for x in w) / 1000 + 2 * len(w)


def refine(answers, tests, root, data, deadline, log, pack):
    """Mutates answers {commit: workers of test indices}."""
    baseline = os.path.join(HERE, "cache", "tpcds_digest.json")
    dmap = os.path.join(HERE, "maps", "tpcds_d.json")
    if not (os.path.exists(baseline) and os.path.exists(dmap)):
        return
    idx = next((i for i, t in enumerate(tests) if t["id"] == TID), None)
    if idx is None:
        return
    with open(baseline) as fh:
        if not isinstance(json.load(fh), list):
            return
    with open(dmap) as fh:
        downstream = json.load(fh)
    seconds = [t["seconds"] for t in tests]
    for c in sorted(answers):
        workers = answers[c]
        flat = [n for w in workers for n in w]
        if idx not in flat:
            continue
        rest = [n for n in flat if n != idx]
        repacked = pack(rest, seconds) if rest else []
        if _cost(workers, seconds) - _cost(repacked, seconds) < MIN_GAIN:
            continue
        if time.monotonic() > deadline - 30:
            log("tpcds proof: no time left")
            return
        try:
            with open(os.path.join(data, "commits", c + ".patch"), encoding="utf-8", errors="replace") as fh:
                files = eligible(root, fh.read(), downstream)
        except Exception as e:  # noqa: BLE001
            log("tpcds proof:", c, repr(e))
            continue
        if files is None:
            continue
        t0 = time.monotonic()
        if digest_equal(root, files, baseline, min(60.0, deadline - 25 - time.monotonic())):
            answers[c] = repacked
            log("tpcds proof:", c, "test 1106 left out, %.2fs" % (time.monotonic() - t0))
        else:
            log("tpcds proof:", c, "kept, %.2fs" % (time.monotonic() - t0))
