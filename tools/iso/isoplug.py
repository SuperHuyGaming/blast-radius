"""pytest plugin: at session end, write the sqlglot modules this process imported."""

import json
import os
import sys


def pytest_sessionfinish(session, exitstatus):
    mods = sorted(m for m in list(sys.modules) if m == "sqlglot" or m.startswith("sqlglot."))
    with open(os.environ["ISO_OUT"], "w") as f:
        json.dump(mods, f)
