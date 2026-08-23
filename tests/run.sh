#!/usr/bin/env bash
# Full test suite: shell smoke checks, then the Python characterization
# suite. Exits nonzero if either fails. Hermetic: isolates state under a
# throwaway XDG_STATE_HOME and skips the network-touching quota snapshot.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_TMP="$(mktemp -d "${TMPDIR:-/tmp}/ssa-run.XXXXXX")"
trap 'rm -rf "$RUN_TMP"' EXIT

export SSA_NO_QUOTA_SNAPSHOT=1
export XDG_STATE_HOME="$RUN_TMP/state"
export XDG_CACHE_HOME="$RUN_TMP/cache"
mkdir -p "$XDG_STATE_HOME" "$XDG_CACHE_HOME"

status=0

echo "== tests/smoke.sh =="
if ! bash "$ROOT/tests/smoke.sh"; then
  echo "run.sh: tests/smoke.sh FAILED" >&2
  status=1
fi

echo
echo "== python3 -m unittest discover -s tests -v =="
if ! (cd "$ROOT" && python3 -m unittest discover -s tests -v); then
  echo "run.sh: unittest suite FAILED" >&2
  status=1
fi

exit "$status"
