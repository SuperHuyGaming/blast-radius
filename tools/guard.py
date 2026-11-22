"""Run a test load with a memory guard.

usage: guard.py <cmd...>
The command runs in its own process group with PYTHON_CPU_COUNT=2 (so spawn pools start
2 workers); the whole group is killed once its RSS passes 4 GB. Exit status: the
command's, or 137 when the guard killed it.
"""

import os
import signal
import subprocess
import sys

RAM_LIMIT_KB = 4 * 1024 * 1024


def group_rss_kb(pgid):
    try:
        ps = subprocess.run(["ps", "-A", "-o", "pgid=,rss="], capture_output=True, text=True).stdout
    except OSError:  # fork refused on a full shared box: check again next round
        return 0
    total = 0
    for ln in ps.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0] == str(pgid):
            total += int(parts[1])
    return total


def main():
    env = dict(os.environ, PYTHON_CPU_COUNT="2")
    p = subprocess.Popen(sys.argv[1:], env=env, start_new_session=True)
    killed = False
    while True:
        try:
            p.wait(timeout=2)
            break
        except subprocess.TimeoutExpired:
            pass
        if group_rss_kb(p.pid) > RAM_LIMIT_KB:
            killed = True
            break
    try:
        os.killpg(p.pid, signal.SIGKILL)  # the tree over the limit, or strays left by the run
    except ProcessLookupError:
        pass
    p.wait()
    if killed:
        print("guard: killed, RSS over 4 GB", file=sys.stderr)
        sys.exit(137)
    sys.exit(p.returncode)


if __name__ == "__main__":
    main()
