#!/bin/bash
# usage: runrec.sh <tree> <outdir> [extra pytest args]
T=$1; O=$2; shift 2
cd "$T" || exit 1
rm -rf "${O:?}"; mkdir -p "$O"
SKIP_INTEGRATION=0 CLSREC_OUT="$O" PYTHONPATH="$T:$(cd "$(dirname "$0")" && pwd)" \
  "${PY:-python}" -m pytest tests -p no:cacheprovider -p clsrec -q -n 2 -rfE "$@" > "$O/pytest.log" 2>&1
echo "EXIT $?" >> "$O/pytest.log"
