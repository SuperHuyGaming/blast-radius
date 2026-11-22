#!/bin/sh
# Runs selector.py without Docker and scores it.
#   BLAST_DATA  dir with commits/ and tests.json (default /data)
#   BLAST_SRC   pristine sqlglot checkout at the pinned commit (default /app/sqlglot-src)
#   BLAST_ANSWERS  answers.json to score against (default $BLAST_DATA/answers.json)
# CPU is the user+sys of the selector and everything it starts, as make cpu counts it.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
export BLAST_DATA="${BLAST_DATA:-/data}"
export BLAST_SRC="${BLAST_SRC:-/app/sqlglot-src}"
answers="${BLAST_ANSWERS:-$BLAST_DATA/answers.json}"
output="$here/output.native.jsonl"

cpu=$(PYTHONDONTWRITEBYTECODE=1 python3 -c '
import resource, subprocess, sys
with open(sys.argv[1], "w") as out:
    subprocess.run(sys.argv[2:], stdout=out, check=True)
usage = resource.getrusage(resource.RUSAGE_CHILDREN)
print(usage.ru_utime + usage.ru_stime)
' "$output" python3 "$here/selector.py")
python3 "$here/score.py" "$BLAST_DATA" "$answers" "$output" --cpu-seconds "$cpu"
