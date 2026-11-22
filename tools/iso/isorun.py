"""Run every test alone in a fresh process and record the sqlglot modules it imports.

usage: isorun.py <sqlglot tree> <tests.json> <out.json> [jobs]
Output: {"mods": [module...], "tests": {test id: [module index...] | null}, "rc": {test id: exit code}}

A test that imports module M when it is the only test in its process is a test that
asks for M. In a shared process the lazy loaders (dialects, optimizer rules) and the
registries they fill hide that, because an earlier test did the import.
"""

import glob
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))


nkids = [0]


def main():
    tree, tests_json, out = sys.argv[1:4]
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    tree = os.path.realpath(tree)
    with open(tests_json) as f:
        ids = [t["id"] for t in json.load(f)["tests"]]
    tmp = tempfile.mkdtemp(prefix="isorun-")
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=tree + os.pathsep + HERE, PYTHONDONTWRITEBYTECODE="1")
    # test_lazy_load runs a bare `python`
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")

    def run(job):
        n, tid = job
        res = os.path.join(tmp, "%d.json" % n)
        cmd = [sys.executable, "-m", "pytest", tid, "-p", "no:cacheprovider", "-p", "isoplug", "-q"]
        p = subprocess.Popen(cmd, cwd=tree, env=dict(env, ISO_OUT=res), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            rc = p.wait(timeout=600)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            rc = -1
        try:
            with open(res) as f:
                mods = json.load(f)
            os.unlink(res)
        except (OSError, ValueError):
            mods = None
        # modules its spawned workers imported (sitecustomize.py); they count as asked for by the test
        for fn in glob.glob(res + ".child.*"):
            with open(fn) as f:
                kids = [line.strip() for line in f if line.strip()]
            os.unlink(fn)
            if mods is not None:
                mods = sorted(set(mods) | set(kids))
                nkids[0] += fn != "%s.child.%d" % (res, p.pid)
        return tid, rc, mods

    names = {}
    tests = {}
    bad = {}
    with ThreadPoolExecutor(jobs) as ex:
        for k, (tid, rc, mods) in enumerate(ex.map(run, enumerate(ids))):
            if rc != 0 or mods is None:
                bad[tid] = rc
            tests[tid] = None if mods is None else [names.setdefault(m, len(names)) for m in mods]
            if k % 100 == 0:
                print(k, tid, rc, file=sys.stderr, flush=True)
    with open(out, "w") as f:
        json.dump({"mods": list(names), "tests": tests, "rc": bad}, f, separators=(",", ":"))
    print("tests=%d modules=%d nonzero=%d with_children=%d" % (len(tests), len(names), len(bad), nkids[0]), file=sys.stderr)


if __name__ == "__main__":
    main()
