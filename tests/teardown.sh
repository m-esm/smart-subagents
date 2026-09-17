#!/usr/bin/env bash
# Shared teardown for tests/smoke.sh and tests/run.sh. Source it, then:
#   trap 'ssa_teardown "$SOME_TMP"' EXIT
#
# A stopped background run leaves its detached bg-run wrapper and watchdog
# finishing (verify, outcome.json, pycache) inside the temp dir for a moment.
# A bare `rm -rf` racing those writes fails with "Directory not empty" on
# macOS, and under `set -e` that failure became the script's exit status even
# after every check had passed. So: wait for the stragglers (bounded), kill
# what is left, retry the rm, and always exit with the status the checks earned.

# Pids still tied to the temp dir: any process whose command line names it
# (the wrapper runs `bg-run --dir <tmp>/...`, the fixture workers live under
# it), plus every process group recorded in a worker.pgid below it.
_ssa_tmp_procs() {
  local tmp="$1" tag pgid
  tag="$(basename "$tmp")"
  {
    pgrep -f -- "$tag" 2>/dev/null
    if [[ -d "$tmp" ]]; then
      find "$tmp" -name worker.pgid -type f 2>/dev/null | while IFS= read -r f; do
        pgid="$(tr -cd '0-9' <"$f" 2>/dev/null)"
        [[ -n "$pgid" ]] && pgrep -g "$pgid" 2>/dev/null
      done
    fi
  } | sort -u | grep -vx "$$" || true
}

ssa_teardown() {
  local rc=$? tmp="${1:-}" tries=0 pids=""
  trap - EXIT
  set +e
  if [[ -n "$tmp" && -d "$tmp" ]]; then
    # Up to 5 s for recorded workers and wrappers to exit on their own.
    while (( tries < 25 )); do
      pids="$(_ssa_tmp_procs "$tmp")"
      [[ -n "$pids" ]] || break
      sleep 0.2
      tries=$(( tries + 1 ))
    done
    if [[ -n "$pids" ]]; then
      # shellcheck disable=SC2086
      kill -KILL $pids 2>/dev/null
      sleep 0.2
    fi
    tries=0
    while (( tries < 5 )); do
      rm -rf "$tmp" 2>/dev/null
      [[ -e "$tmp" ]] || break
      sleep 0.2
      tries=$(( tries + 1 ))
    done
    [[ ! -e "$tmp" ]] || echo "teardown: left $tmp behind (still in use)" >&2
  fi
  exit "$rc"
}
