"""Loaded by every python process the iso run starts, children included (tools/iso is on PYTHONPATH).

A test whose work runs in spawned workers (ProcessPoolExecutor with the spawn context, a bare
`python` subprocess) imports modules the pytest process never sees; isoplug only reads the
pytest process. Each process (the pytest one too, which only repeats isoplug's list) appends the sqlglot modules it imports to <ISO_OUT>.child.<pid>.
The audit hook writes on import, so a worker killed at shutdown still leaves its list.
"""

import os
import sys

_out = os.environ.get("ISO_OUT")
if _out:
    _path = "%s.child.%d" % (_out, os.getpid())
    _seen = set()

    def _hook(event, args):
        if event != "import":
            return
        name = args[0]
        if (name == "sqlglot" or name.startswith("sqlglot.")) and name not in _seen:
            _seen.add(name)
            try:
                with open(_path, "a") as f:
                    f.write(name + "\n")
            except OSError:
                pass

    sys.addaudithook(_hook)
