"""Earlier selectors, each 0 misses on the official set, run beside this one.

Every selector here returns a superset of the failing tests, so their
intersection does too and is never larger than any one of them. The earlier
selectors run as child processes while this one refines; a selector that
fails, times out or leaves out a commit is ignored for that commit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.environ.get("BR_LEGACY", os.path.join(os.path.dirname(HERE), "legacy"))


def start(data: str, root: str, log) -> list:
    procs = []
    if os.environ.get("BR_LEGACY_OFF") == "1" or not os.path.isdir(LEGACY):
        return procs
    for name in sorted(os.listdir(LEGACY)):
        app = os.path.join(LEGACY, name)
        if not os.path.isfile(os.path.join(app, "selector.py")):
            continue
        env = dict(os.environ, BLAST_DATA=data, BLAST_APP=app, BLAST_SRC=root)
        try:
            p = subprocess.Popen([sys.executable, os.path.join(app, "selector.py")], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            procs.append((name, p))
        except Exception as exc:
            log("legacy", name, "did not start:", repr(exc))
    return procs


def poll_finished(procs: list, commits, log) -> list:
    """Collect from any child process that has already finished without blocking or killing."""
    results = []
    for item in list(procs):
        name, p = item
        if p.poll() is not None:
            procs.remove(item)
            try:
                out, _ = p.communicate(timeout=1.0)
                if p.returncode == 0:
                    got = {}
                    for line in out.splitlines():
                        if line.strip():
                            d = json.loads(line)
                            got[d["commit"]] = {int(i) for w in d["workers"] for i in w}
                    results.append(got)
                    log("legacy", name, f"finished early, answered {len(got)} commits")
            except Exception as exc:
                log("legacy", name, "poll error:", repr(exc))
    return results


def collect(procs, commits, deadline: float, log) -> list:
    """Return one {commit: set(test numbers)} per selector that finished cleanly."""
    results = []
    for name, p in procs:
        try:
            out, _ = p.communicate(timeout=max(1.0, deadline - time.monotonic()))
            if p.returncode != 0:
                raise RuntimeError(f"exit {p.returncode}")
            got = {}
            for line in out.splitlines():
                if line.strip():
                    d = json.loads(line)
                    got[d["commit"]] = {int(i) for w in d["workers"] for i in w}
            missing = [c for c in commits if c not in got]
            if missing:
                log("legacy", name, f"left out {len(missing)} commits; ignoring those")
            results.append(got)
            log("legacy", name, f"answered {len(got)} commits")
        except Exception as exc:
            log("legacy", name, "ignored:", repr(exc))
            try:
                p.kill()
            except Exception:
                pass
    return results
