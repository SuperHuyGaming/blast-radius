"""Blast Radius selector: for each patch, the tests that can fail, packed onto workers.

Reads /data (or $BLAST_DATA) and the pristine sqlglot source at /app/sqlglot-src
(or $BLAST_SRC). Writes one JSON line per commit to stdout, logs to stderr.
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import blastsel  # noqa: E402

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SRC = os.environ.get("BLAST_SRC", "/app/sqlglot-src")
MAPS = Path(os.environ.get("BLAST_MAPS", str(HERE / "maps")))
VERBOSE = os.environ.get("BLAST_VERBOSE") == "1"


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_nums = sorted(ms)
    maps = blastsel.Maps(str(MAPS / "cov.bin"), tests)
    clsmap = None
    try:
        import clsmap as _clsmap  # noqa: F401

        clsmap = _clsmap
    except ImportError:
        pass
    an = blastsel.Analyzer(maps, SRC, clsmap)
    out = []
    patches = sorted((DATA / "commits").glob("*.patch"))
    units = None
    runner = None
    unit_num = None
    try:
        import subrun

        units = subrun.load_units(str(MAPS))
        unit_num = next(t["number"] for t in tests if t["id"] == units["test"])
        maps.add_units(units["test"], units["n"], units["lines"], units["pre"])
    except Exception as e:  # no unit map, no runs: plain selection
        units = None
        blastsel.log(f"units off: {e!r}")
    ids = {t["number"]: t["id"] for t in tests}
    files_of = {t["number"]: t["file"] for t in tests}
    suite = None
    suite_cpu = None
    try:
        suite_cpu = subrun.load_suite_cpu(str(MAPS))
        import importlib.util

        if importlib.util.find_spec("pytest") is None:  # the runs need it; without it the selection stands
            raise ImportError("no pytest")
    except Exception as e:
        suite_cpu = None
        blastsel.log(f"suite runs off: {e!r}")
    for patch in patches:
        cid = patch.stem
        res = None
        try:
            text = patch.read_text(encoding="utf-8", errors="surrogateescape")
            res = an.analyze(text)
            nums = all_nums if res.all else sorted(maps.to_numbers(res.bits))
            notes = res.notes
        except Exception as e:  # never die: run everything
            nums = all_nums
            notes = [f"crash {e!r}"]
        workers, cost = blastsel.pack([(n, ms[n]) for n in nums])
        if units is not None and res is not None and unit_num in nums:
            try:
                rest = [n for n in nums if n != unit_num]
                workers2, cost2 = blastsel.pack([(n, ms[n]) for n in rest])
                files = subrun.patched_files(SRC, blastsel.parse_patch(text), blastsel.apply_hunks)
                if files is None:
                    notes.append("tpcds: patch not runnable")
                else:
                    keep = list(range(units["n"])) if res.all else maps.units_of(res.bits)
                    if runner is None:
                        runner = subrun.UnitRunner(SRC, units, len(patches))
                    passed, why = runner.passes(files, keep, (cost - cost2) / 1000)
                    notes.append("tpcds: " + why)
                    if passed:
                        nums, workers, cost = rest, workers2, cost2
            except Exception as e:  # keep the test
                notes.append(f"tpcds run crash {e!r}")
        if res is not None and not res.all and nums and suite_cpu is not None:
            try:
                files = subrun.patched_files(SRC, blastsel.parse_patch(text), blastsel.apply_hunks)
                # library code only: a fixture or config edit changes what the tests read, not what they import
                if files and all(p.startswith("sqlglot/") and p.endswith(".py") for p in files):
                    kept = [n for n in nums if n == unit_num]  # far too slow to run here
                    if suite is None:
                        suite = subrun.SuiteRunner(SRC, suite_cpu, ids, files_of, len(patches))
                    left, why = suite.prove(files, [n for n in nums if n != unit_num], lambda sel: blastsel.pack([(n, ms[n]) for n in sel + kept])[1])
                    notes.append("suite: " + why)
                    if left is not None:
                        nums = sorted(left + kept)
                        workers, cost = blastsel.pack([(n, ms[n]) for n in nums])
            except Exception as e:  # keep the selection
                notes.append(f"suite run crash {e!r}")
        extra = 0
        try:  # fill only appends within the cost, so a partial fill is still a valid line
            if len(nums) < len(all_nums):
                for tier in blastsel.fill_candidates(maps, text, set(nums)):
                    extra += blastsel.fill(workers, ms, sorted(tier, key=ms.__getitem__))
        except Exception as e:
            notes.append(f"fill crash {e!r}")
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        if VERBOSE:
            blastsel.log(f"{cid}: {len(nums)} tests +{extra} fill, {cost / 1000:.3f}s; " + " | ".join(notes))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    if suite is not None:
        blastsel.log(f"suite runs: {suite.runs} runs {suite.cpu:.1f}s cpu, scale {suite.scale:.2f}, batch wall {time.monotonic() - subrun.STARTED:.1f}s")
    if runner is not None:
        blastsel.log(f"tpcds runs: {runner.cpu:.1f}s cpu, scale {runner.scale:.2f}, batch wall {time.monotonic() - subrun.STARTED:.1f}s")


if __name__ == "__main__":
    main()
