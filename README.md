# Blast Radius entry (team mcarkade)

For each patch to sqlglot, pick the tests that can catch it, run the cheap ones
against the patched tree inside the container, and report only the tests that fail.

## How it works

1. `data/map.json.gz`: per-test line coverage of sqlglot at the pinned commit
   (eb11aec), plus the files each test reads and the dialects it instantiates.
   Built from the pinned commit only, with `mapbuild/` tooling (coverage.py
   dynamic contexts).
2. Build time (`br/index_build.py`): turns the map and the sqlglot source into a
   per-line table. A change on a runtime line selects the tests that run its
   enclosing statement; a change to a class or module constant selects the tests
   that read that name; fixture and test-file changes select their readers.
3. Run time (`br/main.py`): parses each patch, drops edits that leave the syntax
   tree unchanged, builds a symlink overlay of the patched tree in `/tmp`, forks
   a child per patch, runs the candidate tests with `unittest`, and prints the
   ones that fail, split across workers to minimise the busiest worker plus the
   per-worker cost.
4. Edits that cannot change behaviour are skipped by program analysis, not by
   lookup: syntax-tree-equal edits, and `is`/`==` swaps against `None` or an enum
   member (no `__eq__` in sqlglot is true for `None`; enum members are singletons).
5. Cost policy: for each patch, verify a test when the measured CPU to run it is
   below what reporting it unverified would add to the schedule; otherwise report
   it. Tests the runner cannot reproduce faithfully (binary fixtures, the C build,
   pytest-only configuration) are always reported.
6. `br/exec_cache.py` parses the TPC-H/TPC-DS CSV fixtures once at build time, so a
   child running executor tests skips that setup when the patch leaves those files,
   the test module and its helpers untouched.

## Third-party code

- [sqlglot](https://github.com/tobymao/sqlglot) (MIT): the project under test,
  downloaded at the pinned commit during the build.
- [coverage.py](https://github.com/nedbat/coveragepy) (Apache 2.0): used offline to build the map.
- Python packages from the organizers' `requirements.lock`.

All other code was written for this event. MIT licensed.
