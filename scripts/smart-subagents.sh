#!/usr/bin/env bash
# smart-subagents.sh: orchestration helpers for the smart-subagents supervisor.
# Keeps the supervisor's tokens on judgment; shell does the mechanical work.
set -euo pipefail

# Resolve alongside this script so the tool works from a plugin dir, a clone, or
# a symlink on PATH. CLAUDE_PLUGIN_ROOT wins when Claude Code sets it.
_self="${BASH_SOURCE[0]}"
while [[ -L "$_self" ]]; do
  _link="$(readlink "$_self")"
  [[ "$_link" == /* ]] && _self="$_link" || _self="$(dirname "$_self")/$_link"
done
SCRIPT_DIR="$(cd "$(dirname "$_self")" && pwd)"
SSA_ROOT="${CLAUDE_PLUGIN_ROOT:-$(dirname "$SCRIPT_DIR")}"
# Absolute path to this script, for the detached background wrapper re-exec.
SSA_SELF="${SCRIPT_DIR}/$(basename "$_self")"

USAGE_PY="${SSA_USAGE_PY:-${SSA_ROOT}/scripts/ai-cli-usage.py}"
[[ -f "$USAGE_PY" ]] || USAGE_PY="${SCRIPT_DIR}/ai-cli-usage.py"

# The runtime: registry, worker adapters, task state machine. Everything this
# script used to know per CLI lives behind it.
SSA_CLI_PY="${SSA_CLI_PY:-${SSA_ROOT}/scripts/ssa/cli.py}"
[[ -f "$SSA_CLI_PY" ]] || SSA_CLI_PY="${SCRIPT_DIR}/ssa/cli.py"

# Work dir holds briefs, worktrees and worker logs: private, never world-readable.
SSA_WORK_DIR="${SSA_WORK_DIR:-${TMPDIR:-/tmp}/smart-subagents}"

# Ledger of dispatch outcomes: state, not cache, so it survives a cache wipe.
SSA_STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/smart-subagents"
SSA_LEDGER="${SSA_LEDGER:-${SSA_STATE_DIR}/outcomes.jsonl}"

die() { echo "smart-subagents: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing $1"; }

# Briefs, worktrees and worker logs live here. On a shared /tmp another user
# can own (or pre-create) the path, and `chmod 700` on someone else's dir is a
# silent no-op, so ownership is checked rather than assumed.
_ssa_ensure_work_dir() {
  mkdir -p "$SSA_WORK_DIR" 2>/dev/null || die "cannot create $SSA_WORK_DIR"
  [[ -O "$SSA_WORK_DIR" ]] || die "$SSA_WORK_DIR is not owned by this user; refusing to write briefs and worktrees into it (set SSA_WORK_DIR to a path you own)"
  chmod 700 "$SSA_WORK_DIR" 2>/dev/null || true
}

# Worktrees are siblings of the task dirs, never children of one: a worker with
# its cwd in the worktree must not be able to reach ../verify-cmds.txt,
# ../scope.txt or ../outcome.json and forge its own verification.
_ssa_wt_path() {
  printf '%s/wt/%s' "$SSA_WORK_DIR" "$1"
}

# Some clones never return from `git status --porcelain -uall` (untracked
# explosion) or return a list so large the command is unusable. Probe with a
# timeout and an untracked cap, then refuse rather than hang. `status -uno`
# is fine on those trees.
# SSA_GIT_STATUS_TIMEOUT seconds, default 5.
# SSA_GIT_STATUS_UALL_MAX untracked porcelain lines, default 1000.
_ssa_git_status_porcelain_uall() {
  local repo="$1" dest="${2:-}" who="${3:-smart-subagents}"
  local secs="${SSA_GIT_STATUS_TIMEOUT:-5}"
  local max_u="${SSA_GIT_STATUS_UALL_MAX:-1000}"
  need python3
  python3 - "$repo" "$dest" "$secs" "$who" "$max_u" <<'PY'
import subprocess
import sys

repo, dest, secs_s, who, max_s = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
try:
    secs = float(secs_s)
except ValueError:
    secs = 5.0
try:
    max_u = int(float(max_s))
except ValueError:
    max_u = 1000
inside = subprocess.run(
    ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
if inside.returncode != 0:
    sys.exit(0)
try:
    ran = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
        timeout=secs,
    )
except subprocess.TimeoutExpired:
    sys.stderr.write(
        "smart-subagents: %s: git status -uall timed out after %ss in %s "
        "(this clone hangs on -uall; refuse rather than hang)\n"
        % (who, int(secs) if secs == int(secs) else secs, repo)
    )
    sys.exit(1)
text = ran.stdout or ""
untracked = sum(1 for line in text.splitlines() if line.startswith("??"))
if max_u >= 0 and untracked > max_u:
    sys.stderr.write(
        "smart-subagents: %s: git status -uall is unusable in %s "
        "(%s untracked; this clone hangs on -uall; refuse rather than hang)\n"
        % (who, repo, untracked)
    )
    sys.exit(1)
if dest:
    with open(dest, "w") as out:
        out.write(text)
sys.exit(0)
PY
}

# Every byte printed by a command lands in the supervisor's context, so any
# output whose size the repo controls is clipped and spilled to a file.
# _ssa_clip_file SRC LABEL MAX FULL -> print LABEL and at most MAX lines.
_ssa_clip_file() {
  local src="$1" label="$2" max="${3:-200}" full="$4" total=0
  printf '### %s\n' "$label"
  [[ -f "$src" ]] || return 0
  total="$(wc -l <"$src" | tr -d ' ')"
  [[ "$total" =~ ^[0-9]+$ ]] || total=0
  if [[ -n "$full" ]]; then
    { printf '### %s\n' "$label"; cat "$src"; } >>"$full"
  fi
  head -n "$max" "$src"
  if (( total > max )); then
    printf '(+%s more lines, full text in %s)\n' "$(( total - max ))" "$full"
  fi
}

# Implement briefs must declare how structural discovery happened. Workers
# execute the brief only, so the supervisor has to carry a pack or a skip.
# SSA_STRUCTURAL_LEGACY=1 is a temporary rollback, not the default.
_ssa_require_structural() {
  local dir="$1" brief="$2"
  local kind
  kind="$(tr -d '[:space:]' <"$dir/kind.txt" 2>/dev/null || true)"
  case "$kind" in
    plan) echo "none" >"$dir/route.txt"; return 0 ;;
  esac
  if [[ "${SSA_STRUCTURAL_LEGACY:-}" == "1" ]]; then
    echo "legacy" >"$dir/route.txt"
    echo "dispatch: SSA_STRUCTURAL_LEGACY=1, structural brief gate skipped" >&2
    return 0
  fi
  if ! grep -qE '^## Structural (discovery|context)[[:space:]]*$' "$brief"; then
    die "dispatch: brief missing ## Structural discovery (CGC: <abs pack> or CGC-SKIP: reason; route=ast-grep|rg|none; evidence=...). Set SSA_STRUCTURAL_LEGACY=1 only to roll back"
  fi
  local cgc_line skip_line
  cgc_line="$(grep -E '^CGC:[[:space:]]+' "$brief" | head -1 || true)"
  skip_line="$(grep -E '^CGC-SKIP:[[:space:]]+' "$brief" | head -1 || true)"
  if [[ -n "$cgc_line" && -n "$skip_line" ]]; then
    die "dispatch: brief has both CGC: and CGC-SKIP:; pick one"
  fi
  if [[ -n "$cgc_line" ]]; then
    local pack
    pack="$(sed -E 's/^CGC:[[:space:]]+//' <<<"$cgc_line" | tr -d '\r')"
    [[ "$pack" == /* ]] || die "dispatch: CGC pack must be an absolute path"
    [[ -f "$pack" ]] || die "dispatch: CGC pack not found: $pack"
    if grep -q 'NOT IN GRAPH' "$pack" && ! grep -qE '^== .+  .+:[0-9]+' "$pack"; then
      die "dispatch: CGC pack is a miss stub (NOT IN GRAPH): $pack"
    fi
    echo "cgc" >"$dir/route.txt"
    return 0
  fi
  if [[ -n "$skip_line" ]]; then
    local route
    route="$(sed -nE 's/.*[[:space:]]route=([a-z-]+).*/\1/p' <<<"$skip_line" | head -1)"
    case "$route" in
      ast-grep|rg|none) echo "$route" >"$dir/route.txt" ;;
      *) die "dispatch: CGC-SKIP must include route=ast-grep|rg|none" ;;
    esac
    if ! grep -qE 'evidence=' <<<"$skip_line"; then
      die "dispatch: CGC-SKIP must include evidence=<artifact or literal>"
    fi
    return 0
  fi
  die "dispatch: Structural discovery section has neither CGC: nor CGC-SKIP:"
}

# --- the runtime seam ---------------------------------------------------------
# One registry entry describes a worker; this script never learns a CLI's name.
# Everything below goes through scripts/ssa/cli.py.

_ssa() { python3 "$SSA_CLI_PY" "$@"; }

# _ssa_build WORKER MODE [args...] -> fills _BC_* for one worker invocation.
# The command crosses the process boundary NUL-separated because that is the
# only way to move a list of arbitrary strings intact on bash 3.2.
_BC_BIN=""; _BC_CWD=""; _BC_STDIN=""; _BC_ENV_SCRUB=""; _BC_OUTPUT_MODE=""
_BC_WRITE_OK=""; _BC_SANDBOX=""; _BC_ARGV=()
_ssa_build() {
  local worker="$1" mode="$2"; shift 2
  local tok n=0 tmp
  _BC_BIN=""; _BC_CWD=""; _BC_STDIN=""; _BC_ENV_SCRUB=""; _BC_OUTPUT_MODE=""
  _BC_WRITE_OK=""; _BC_SANDBOX=""; _BC_ARGV=()
  tmp="$(mktemp "${TMPDIR:-/tmp}/ssa-build.XXXXXX")"
  if ! _ssa build-command --worker "$worker" --mode "$mode" --nul "$@" >"$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  while IFS= read -r -d '' tok; do
    case "$n" in
      0) _BC_BIN="$tok" ;;
      1) _BC_CWD="$tok" ;;
      2) _BC_STDIN="$tok" ;;
      3) _BC_ENV_SCRUB="$tok" ;;
      4) _BC_OUTPUT_MODE="$tok" ;;
      5) _BC_WRITE_OK="$tok" ;;
      6) _BC_SANDBOX="$tok" ;;
      *) _BC_ARGV+=("$tok") ;;
    esac
    n=$(( n + 1 ))
  done <"$tmp"
  rm -f "$tmp"
  (( n >= 7 )) || return 1
}

# Run the built command. Caller owns every redirection.
_ssa_run_worker() {
  if [[ "$_BC_ENV_SCRUB" == "1" ]]; then
    ( cd "${_BC_CWD:-.}" \
      && env -i HOME="$HOME" PATH="$PATH" TMPDIR="${TMPDIR:-/tmp}" \
        TERM="${TERM:-dumb}" "$_BC_BIN" ${_BC_ARGV[@]+"${_BC_ARGV[@]}"} )
  elif [[ -n "$_BC_CWD" ]]; then
    ( cd "$_BC_CWD" && "$_BC_BIN" ${_BC_ARGV[@]+"${_BC_ARGV[@]}"} )
  else
    "$_BC_BIN" ${_BC_ARGV[@]+"${_BC_ARGV[@]}"}
  fi
}

# Lifecycle bookkeeping. Neither an event nor a refused transition is allowed
# to fail the command it annotates: the work is the point, the record is the
# audit trail. A refusal is loud on stderr instead.
_ssa_event() {
  local dir="$1"; shift
  [[ -d "$dir" ]] || return 0
  _ssa event --dir "$dir" --quiet "$@" 2>/dev/null || true
}

_ssa_state() {
  local dir="$1" to="$2"; shift 2
  [[ -d "$dir" ]] || return 0
  if _ssa transition --dir "$dir" --to "$to" --quiet "$@" 2>/dev/null; then
    return 0
  fi
  # A retry re-enters the lifecycle through "picked": that edge exists from
  # every end of a run, so an ordinary second dispatch is not a desync.
  if [[ "$to" == "running" ]] \
      && _ssa transition --dir "$dir" --to picked --quiet 2>/dev/null \
      && _ssa transition --dir "$dir" --to "$to" --quiet "$@" 2>/dev/null; then
    return 0
  fi
  # Swallowing the refusal is what froze 1788291814-77216 at "running" forever.
  # Record the drift, then force the record to say what actually happened.
  printf '%s refused %s -> %s\n' "$(_utc)" "$(_task_state "$dir")" "$to" \
    >>"$dir/state-desync.txt"
  echo "smart-subagents: state refused ->$to for $dir (recorded in $dir/state-desync.txt)" >&2
  _ssa_state_force "$dir" "$to" || true
}

# The forced write. cli.py has no --force flag, so the transition goes through
# ssa.state directly; unknown state names are still refused there.
_ssa_state_force() {
  local dir="$1" to="$2"
  python3 - "$SSA_CLI_PY" "$dir" "$to" <<'PY' 2>/dev/null || return 1
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[1]))))
from ssa import state  # noqa: E402

state.transition(sys.argv[2], sys.argv[3], force=True)
PY
}

# --- small artifact helpers ---------------------------------------------------
# Task dirs are flat files, and every one of them is optional: a task killed
# between mint and dispatch still has to render.

_read1() {
  # _read1 FILE [DEFAULT] -> first line, or DEFAULT when missing/empty
  local f="$1" d="${2:-}" v=""
  if [[ -f "$f" ]]; then
    v="$(head -n1 "$f" 2>/dev/null || true)"
  fi
  [[ -n "$v" ]] || v="$d"
  printf '%s' "$v"
}

_mtime() {
  # BSD stat and GNU stat disagree; try both, print 0 when neither works.
  local f="$1" v=""
  # GNU first: BSD stat has no -c and fails cleanly, while GNU stat -f exits 0
  # with filesystem text that would poison the arithmetic below.
  v="$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || true)"
  [[ "$v" =~ ^[0-9]+$ ]] || v=0
  printf '%s' "$v"
}

_age_human() {
  # seconds -> compact age (45s, 12m, 3h, 2d)
  local s="${1:-0}"
  [[ "$s" =~ ^[0-9]+$ ]] || s=0
  if (( s < 60 )); then printf '%ss' "$s"
  elif (( s < 3600 )); then printf '%sm' $(( s / 60 ))
  elif (( s < 86400 )); then printf '%sh' $(( s / 3600 ))
  else printf '%sd' $(( s / 86400 )); fi
}

_now() { date +%s; }

_utc() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

_pid_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

_pid_start() {
  # Process start time, the cheap defense against PID reuse.
  local pid="${1:-}" v=""
  [[ -n "$pid" ]] || return 0
  v="$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//;s/ *$//' || true)"
  printf '%s' "$v"
}

_worker_state() {
  # _worker_state DIR -> none|running|exited|reused|stalled|stopped
  local dir="$1" pid start now
  pid="$(_read1 "$dir/worker.pid")"
  [[ -n "$pid" ]] || { printf 'none'; return 0; }
  if _pid_alive "$pid"; then
    start="$(_read1 "$dir/worker-start.txt")"
    now="$(_pid_start "$pid")"
    # No readable start time for a live pid means we cannot prove the process
    # is still ours, and "probably ours" is how a stranger's pid gets killed.
    if [[ -z "$now" ]]; then
      printf 'reused'; return 0
    fi
    if [[ -n "$start" && "$start" != "$now" ]]; then
      printf 'reused'; return 0
    fi
    printf 'running'; return 0
  fi
  if [[ -f "$dir/stopped.txt" ]]; then printf 'stopped'; return 0; fi
  if [[ -f "$dir/stalled.txt" ]]; then printf 'stalled'; return 0; fi
  printf 'exited'
}

_task_phase() {
  # Phase is inferred from which artifacts exist, never from a status file that
  # a killed worker would leave lying.
  local dir="$1" state
  state="$(_worker_state "$dir")"
  if [[ -f "$dir/outcome.json" ]]; then printf 'verified'; return 0; fi
  if [[ -f "$dir/exit-code.txt" ]]; then printf 'done'; return 0; fi
  if [[ "$state" == "running" ]]; then printf 'running'; return 0; fi
  if [[ ! -f "$dir/brief.md" ]]; then printf 'minted'; return 0; fi
  if [[ ! -f "$dir/stdout.log" ]]; then printf 'briefed'; return 0; fi
  # A pid we know about, no longer alive, and no exit code: the run was cut off.
  case "$state" in
    stopped|stalled|exited|reused) printf 'aborted'; return 0 ;;
  esac
  printf 'running'
}

_diff_summary() {
  # Last line of git diff --stat, squeezed onto one line.
  local dir="$1" line=""
  [[ -f "$dir/diff-stat.txt" ]] || { printf '%s' "-"; return 0; }
  line="$(tail -n1 "$dir/diff-stat.txt" 2>/dev/null | sed 's/^ *//;s/ *$//' || true)"
  [[ -n "$line" ]] || line="-"
  printf '%s' "$line"
}

_task_state() {
  # Recorded lifecycle state, falling back to the artifact inference for task
  # dirs minted before task.json existed. Read with sed rather than a python
  # launch per row: `ls` renders every task dir on the machine.
  local dir="$1" v=""
  if [[ -f "$dir/task.json" ]]; then
    v="$(sed -n 's/^  "state": "\([a-z]*\)".*/\1/p' "$dir/task.json" | head -n1)"
  fi
  [[ -n "$v" ]] || v="$(_task_phase "$dir")"
  printf '%s' "$v"
}

_task_class() {
  local dir="$1"
  printf '%s/%s/%s' "$(_read1 "$dir/size.txt" '?')" \
    "$(_read1 "$dir/difficulty.txt" '?')" "$(_read1 "$dir/kind.txt" '?')"
}

# Init is transactional: a failure after `worktree add` must not leave a
# half-minted task dir and an orphan ssa/<id> branch behind. The trap is armed
# the moment the worktree exists and disarmed on the success path.
_INIT_REPO=""
_INIT_WT=""
_INIT_DIR=""
_INIT_ID=""

_init_rollback() {
  local rc=$? clean=1 st
  [[ -n "$_INIT_WT" ]] || exit "$rc"
  st="$(mktemp "${TMPDIR:-/tmp}/ssa-rollback-status.XXXXXX")"
  if ! _ssa_git_status_porcelain_uall "$_INIT_WT" "$st" "init-rollback" 2>/dev/null \
      || [[ -s "$st" ]]; then
    clean=0
  fi
  rm -f "$st"
  # Only ever remove a worktree this call created and nobody has written to.
  if [[ -d "$_INIT_WT" ]] && (( clean == 1 )); then
    git -C "$_INIT_REPO" worktree remove "$_INIT_WT" >/dev/null 2>&1 || true
    git -C "$_INIT_REPO" branch -D "ssa/${_INIT_ID}" >/dev/null 2>&1 || true
    git -C "$_INIT_REPO" worktree prune >/dev/null 2>&1 || true
    rm -rf "$_INIT_DIR"
    echo "smart-subagents: init failed, rolled back worktree and ssa/${_INIT_ID}" >&2
  else
    echo "smart-subagents: init failed, left $_INIT_DIR in place (worktree not clean)" >&2
  fi
  exit "$rc"
}

cmd_init() {
  local repo="" size="medium" preferred="" difficulty="routine" kind="default"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo) repo="${2:-}"; shift 2 ;;
      --size) size="${2:-}"; shift 2 ;;
      --difficulty) difficulty="${2:-}"; shift 2 ;;
      --kind) kind="${2:-}"; shift 2 ;;
      --prefer) preferred="${2:-}"; shift 2 ;;
      *) die "init: unknown arg $1" ;;
    esac
  done
  [[ -n "$repo" ]] || die "init: --repo required"
  [[ -d "$repo" ]] || die "init: repo not a directory: $repo"
  need git
  need python3

  local task_id dir status_tmp
  status_tmp="$(mktemp "${TMPDIR:-/tmp}/ssa-repo-status.XXXXXX")"
  if ! _ssa_git_status_porcelain_uall "$repo" "$status_tmp" "init"; then
    rm -f "$status_tmp"
    exit 1
  fi
  task_id="$(date +%s)-$$"
  _ssa_ensure_work_dir
  dir="${SSA_WORK_DIR}/${task_id}"
  mkdir -m 700 -p "$dir"
  mv "$status_tmp" "$dir/repo-status.txt"

  echo "$task_id" >"$dir/task-id.txt"
  echo "$repo" >"$dir/repo.txt"
  echo "$size" >"$dir/size.txt"
  echo "$difficulty" >"$dir/difficulty.txt"
  echo "$kind" >"$dir/kind.txt"
  git -C "$repo" rev-parse HEAD >"$dir/base-sha.txt"
  git -C "$repo" rev-parse --abbrev-ref HEAD >"$dir/repo-branch.txt"

  _ssa_state "$dir" minted
  _ssa_event "$dir" --phase minted

  # Parallel: usage + worktree
  python3 "$USAGE_PY" --json --task-size "$size" --difficulty "$difficulty" \
    --task-kind "$kind" \
    >"$dir/usage.json" &
  local usage_pid=$!

  local wt
  wt="$(_ssa_wt_path "$task_id")"
  if git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    mkdir -m 700 -p "$(dirname "$wt")"
    # git worktree prints progress on stdout, keep JSON clean
    git -C "$repo" worktree add "$wt" -b "ssa/${task_id}" >/dev/null 2>&1 \
      || die "init: worktree add failed for $repo"
    echo "$wt" >"$dir/wt.txt"
    _INIT_REPO="$repo"; _INIT_WT="$wt"; _INIT_DIR="$dir"; _INIT_ID="$task_id"
    trap '_init_rollback' EXIT
  else
    echo "NOT_GIT" >"$dir/wt.txt"
    wt=""
  fi

  wait "$usage_pid" || true
  [[ -s "$dir/usage.json" ]] || die "init: usage preflight produced nothing"
  _ssa_state "$dir" preflighted
  _ssa_event "$dir" --phase preflighted --artifact "$dir/usage.json"

  # Pick worker (stdout = single JSON object only)
  python3 - "$dir" "$preferred" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
preferred = (sys.argv[2] or "").strip().lower()
usage = json.loads((d / "usage.json").read_text())
rec = usage.get("recommendation") or {}
ranked = rec.get("ranked") or []
eligible = {r["cli"] for r in ranked}
primary = rec.get("primary_worker")
fallbacks = rec.get("fallback_workers") or []
order = ([primary] if primary else []) + [f for f in fallbacks if f != primary]
# honor preferred if eligible
if preferred and preferred in eligible:
    pick = preferred
    reason = f"parent preferred {preferred} (eligible)"
elif primary:
    pick = primary
    reason = f"primary={primary} score={next((r['score'] for r in ranked if r['cli']==primary), '?')}"
else:
    pick = ""
    reason = "no eligible worker"
pick_doc = {
    "worker": pick,
    "reason": reason,
    "fallbacks": [c for c in order if c != pick],
    "local_labor_ok": rec.get("local_labor_ok"),
    "task_size": rec.get("task_size"),
    "difficulty": rec.get("difficulty"),
    "target_effort": rec.get("target_effort"),
    "cross_review_required": rec.get("cross_review_required"),
    "worker_args": (rec.get("worker_args") or {}).get(pick) or [],
    "all_worker_args": rec.get("worker_args") or {},
    "ranked": ranked,
    "reasons": rec.get("reasons") or [],
}
(d / "worker-args.txt").write_text(
    "\n".join(pick_doc["worker_args"]) + ("\n" if pick_doc["worker_args"] else "")
)
# Per-CLI copies so a later --worker override (or worker.txt edit) can rebind
# without keeping the originally picked CLI's flags (see 1787682071-8525).
for cli, args in (pick_doc.get("all_worker_args") or {}).items():
    args = args or []
    (d / ("worker-args-%s.txt" % cli)).write_text(
        "\n".join(args) + ("\n" if args else "")
    )
(d / "pick.json").write_text(json.dumps(pick_doc, indent=2))
(d / "worker.txt").write_text(pick + ("\n" if pick else ""))
print(json.dumps({"task_id": d.name, "dir": str(d), **pick_doc}, indent=2))
PY

  _ssa_state "$dir" picked
  _ssa_event "$dir" --phase picked --artifact "$dir/pick.json"

  # Success: disarm the rollback.
  trap - EXIT
  _INIT_WT=""; _INIT_DIR=""; _INIT_REPO=""; _INIT_ID=""
}

cmd_pick() {
  local size="medium" out="" preferred="" difficulty="routine" kind="default" explain=""
  local fresh=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --size) size="${2:-}"; shift 2 ;;
      --difficulty) difficulty="${2:-}"; shift 2 ;;
      --kind) kind="${2:-}"; shift 2 ;;
      --out) out="${2:-}"; shift 2 ;;
      --prefer) preferred="${2:-}"; shift 2 ;;
      --fresh) fresh=(--fresh); shift ;;
      --explain) explain=1; shift ;;
      *) die "pick: unknown arg $1" ;;
    esac
  done
  need python3
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/ssa-pick.XXXXXX")"
  # bash 3.2 + set -u: an empty array must be expanded defensively
  if ! python3 "$USAGE_PY" --json --task-size "$size" --difficulty "$difficulty" \
      --task-kind "$kind" \
      ${fresh[@]+"${fresh[@]}"} >"$tmp"; then
    rm -f "$tmp"
    die "pick: usage preflight failed"
  fi
  if [[ -n "$out" ]]; then
    mkdir -p "$out"
    cp "$tmp" "$out/usage.json"
  fi
  # stderr is read by a supervisor, so it is one line unless asked otherwise:
  # the full recommendation is 2 KB+ of JSON nobody reads on the happy path.
  python3 - "$tmp" "$preferred" "${explain:-0}" <<'PY'
import json, sys
usage = json.loads(open(sys.argv[1]).read())
preferred = (sys.argv[2] or "").strip().lower()
explain = sys.argv[3] == "1"
rec = usage.get("recommendation") or {}
ranked = rec.get("ranked") or []
eligible = {r["cli"] for r in ranked}
primary = rec.get("primary_worker")
if preferred and preferred in eligible:
    pick = preferred
elif primary:
    pick = primary
else:
    pick = ""
print(pick or "none")
if explain:
    print(json.dumps(rec, indent=2), file=sys.stderr)
else:
    score = next((r.get("score") for r in ranked if r.get("cli") == pick), None)
    fallbacks = [c for c in (rec.get("fallback_workers") or []) if c != pick]
    print(
        "primary=%s score=%s fallbacks=%s local_labor_ok=%s (--explain for the full JSON)"
        % (pick or "none", score, ",".join(fallbacks) or "-", rec.get("local_labor_ok")),
        file=sys.stderr,
    )
PY
  rm -f "$tmp"
}

_classify_failure() {
  # RC + log tail -> rate-limit | auth | unknown | "" (the run did not fail).
  # The heuristics live in ssa/adapters.py now, one implementation for the
  # shell and for anything else that needs to name a failure.
  local rc="$1" log="$2"
  [[ -f "$log" ]] || return 0
  _ssa classify --exit "$rc" --log "$log" 2>/dev/null || true
}

_maybe_cooldown() {
  # _maybe_cooldown DIR WORKER RC CLASS -> bench the worker when the log said why
  local dir="$1" worker="$2" rc="$3" reason="${4:-}"
  [[ "$rc" != "0" ]] || return 0
  case "$reason" in
    rate-limit|auth) ;;
    *) return 0 ;;
  esac
  printf '%s\n' "$reason" >"$dir/cooldown.txt"
  if [[ -f "$USAGE_PY" ]]; then
    python3 "$USAGE_PY" --cooldown "$worker" --cooldown-reason "$reason" \
      >/dev/null 2>&1 || true
  fi
  echo "cooldown=$worker:$reason"
}

cmd_cooldown() {
  local cli="" clear="" reason="rate-limit" minutes=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cli) cli="${2:-}"; shift 2 ;;
      --clear) clear=1; shift ;;
      --reason) reason="${2:-}"; shift 2 ;;
      --minutes) minutes="${2:-}"; shift 2 ;;
      *) die "cooldown: unknown arg $1" ;;
    esac
  done
  [[ -n "$cli" ]] || die "cooldown: --cli required"
  need python3
  [[ -f "$USAGE_PY" ]] || die "cooldown: no usage script at $USAGE_PY"
  if [[ -n "$clear" ]]; then
    python3 "$USAGE_PY" --clear-cooldown "$cli"
    return $?
  fi
  if [[ -n "$minutes" ]]; then
    python3 "$USAGE_PY" --cooldown "$cli" --cooldown-reason "$reason" \
      --cooldown-minutes "$minutes"
  else
    python3 "$USAGE_PY" --cooldown "$cli" --cooldown-reason "$reason"
  fi
}

# Rebind worker-args.txt to the worker about to launch. Overriding worker.txt
# (or passing --worker) after pick used to leave the original CLI's flags in
# place: grok then got Claude's `--model fable` and died (1787682071-8525).
_ssa_bind_worker_args() {
  local dir="$1" worker="$2"
  local per="$dir/worker-args-$worker.txt"
  if [[ -f "$per" ]]; then
    cp "$per" "$dir/worker-args.txt"
    return 0
  fi
  python3 - "$dir" "$worker" <<'PY' || return $?
import json, sys
from pathlib import Path

d = Path(sys.argv[1])
worker = sys.argv[2]
pick_path = d / "pick.json"
if not pick_path.exists():
    sys.exit(0)
try:
    pick = json.loads(pick_path.read_text())
except (OSError, json.JSONDecodeError):
    sys.exit(0)
all_args = pick.get("all_worker_args") or {}
if worker in all_args:
    args = all_args[worker] or []
    (d / "worker-args.txt").write_text(
        "\n".join(args) + ("\n" if args else "")
    )
    sys.exit(0)
current = []
p = d / "worker-args.txt"
if p.exists():
    current = [l for l in p.read_text().splitlines() if l.strip()]
for cli, args in all_args.items():
    if cli == worker:
        continue
    if args and current == list(args):
        print(
            "smart-subagents: dispatch: worker-args.txt is %s's flags, not %s; "
            "re-run pick --prefer %s" % (cli, worker, worker),
            file=sys.stderr,
        )
        sys.exit(1)
sys.exit(0)
PY
}

cmd_dispatch() {
  local dir="" worker="" background="" mode="implement" resume=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      --worker) worker="${2:-}"; shift 2 ;;
      --background|--bg) background=1; shift ;;
      --resume) resume=1; mode="resume"; shift ;;
      *) die "dispatch: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "dispatch: --dir required"
  local src_repo
  src_repo="$(tr -d '\r\n' <"$dir/repo.txt" 2>/dev/null || true)"
  if [[ -n "$src_repo" && -d "$src_repo" ]]; then
    _ssa_git_status_porcelain_uall "$src_repo" "" "dispatch"
  fi
  worker="${worker:-$(cat "$dir/worker.txt" 2>/dev/null || true)}"
  [[ -n "$worker" ]] || die "dispatch: no worker"
  local brief="$dir/brief.md"
  [[ -f "$brief" ]] || die "dispatch: missing $brief"
  _ssa_require_structural "$dir" "$brief"
  local wt
  wt="$(cat "$dir/wt.txt" 2>/dev/null || true)"
  [[ -n "$wt" && -d "$wt" ]] || die "dispatch: missing worktree ($dir/wt.txt)"
  _ssa_bind_worker_args "$dir" "$worker" || die "dispatch: worker-args do not match $worker"

  # Resume is by session id only: a worker that emitted none cannot be resumed,
  # and a handoff there needs a fresh brief instead.
  local resume_sid=""
  if [[ -n "$resume" ]]; then
    [[ ! -f "$dir/resume-unavailable.txt" ]] \
      || die "dispatch: $dir has no resumable session (resume-unavailable.txt)"
    resume_sid="$(_read1 "$dir/session-id.txt")"
    [[ -n "$resume_sid" ]] || die "dispatch: no session id in $dir/session-id.txt"
  fi

  if [[ -n "$background" ]]; then
    _dispatch_background "$dir" "$worker" "$resume"
    return $?
  fi

  # Quota snapshot before the run: the ledger needs both ends to attribute cost.
  # init's usage.json is minutes to hours old by dispatch time, so the ledger
  # was charging this run for quota someone else spent. Take a fresh one unless
  # the snapshot is already current or the environment forbids the network.
  local usage_age=999999
  if [[ -f "$dir/usage.json" ]]; then
    usage_age=$(( $(_now) - $(_mtime "$dir/usage.json") ))
  fi
  if [[ "${SSA_NO_QUOTA_SNAPSHOT:-}" != "1" && -f "$USAGE_PY" ]] \
      && (( usage_age >= 60 )); then
    python3 "$USAGE_PY" --json --fresh >"$dir/quota-before.json" 2>/dev/null || true
    [[ -s "$dir/quota-before.json" ]] || rm -f "$dir/quota-before.json"
  fi
  if [[ ! -s "$dir/quota-before.json" && -f "$dir/usage.json" ]]; then
    cp "$dir/usage.json" "$dir/quota-before.json" 2>/dev/null || true
  fi

  echo "$worker" >"$dir/worker.txt"
  local log="$dir/stdout.log"
  local sid="" rc=0 failure=""
  local launch_brief
  launch_brief="$(_ssa_stage_worktree_brief "$wt" "$brief")"

  # Difficulty-derived flags come from the recommender, never re-derived here;
  # the registry decides where in the argv they land.
  _ssa_build "$worker" "$mode" \
    --worktree "$wt" --brief "$launch_brief" --output "$dir/last-msg.txt" \
    --args-file "$dir/worker-args.txt" \
    ${resume_sid:+--session-id "$resume_sid"} \
    || die "dispatch: cannot build a $mode command for worker $worker"

  # Capability, not a name: a worker with no sandbox cannot be trusted with a
  # write dispatch just because a worktree looks like containment.
  if [[ "$_BC_WRITE_OK" != "1" ]]; then
    if [[ "${SSA_ALLOW_UNSANDBOXED_WRITE:-}" != "1" && "${SSA_ALLOW_KIMI_WRITE:-}" != "1" ]]; then
      die "dispatch: $worker has no sandbox and write dispatch is blocked; set SSA_ALLOW_UNSANDBOXED_WRITE=1 to override the risk (SSA_ALLOW_KIMI_WRITE=1 still accepted)"
    fi
    _utc >"$dir/write-override.txt"
    _utc >"$dir/kimi-override.txt"
    _ssa_event "$dir" --phase write-override --worker "$worker"
  fi
  [[ -n "$_BC_BIN" && -x "$_BC_BIN" ]] || die "$worker not found at ${_BC_BIN:-<unresolved>}"

  # Job control gives the worker its own process group, so `stop` and the
  # watchdog can end the worker without taking this wrapper (and the artifacts
  # it still owes) down with it. A foreground dispatch used to record no pid at
  # all, which left stop and gc blind to a live worker.
  local wpid=0 wpgid=""
  set -m
  if [[ -n "$_BC_STDIN" ]]; then
    _ssa_run_worker <"$_BC_STDIN" >"$log" 2>&1 &
  else
    _ssa_run_worker </dev/null >"$log" 2>&1 &
  fi
  wpid=$!
  set +m
  echo "$wpid" >"$dir/worker.pid"
  _pid_start "$wpid" >"$dir/worker-start.txt"
  wpgid="$(_pgid_of "$wpid")"
  echo "$wpgid" >"$dir/worker.pgid"
  _ssa_state "$dir" running --worker "$worker" --pid "$wpid"
  _ssa_event "$dir" --phase running --worker "$worker" --pid "$wpid"

  wait "$wpid" || rc=$?

  sid="$(_ssa parse-session --worker "$worker" --log "$log" 2>/dev/null || true)"
  failure="$(_classify_failure "$rc" "$log")"

  echo "$rc" >"$dir/exit-code.txt"
  echo "$sid" >"$dir/session-id.txt"
  _ssa_state "$dir" exited --worker "$worker" --exit "$rc" \
    ${failure:+--failure-class "$failure"} ${sid:+--session-id "$sid"}
  _ssa_event "$dir" --phase exited --worker "$worker" --exit "$rc" \
    ${failure:+--failure-class "$failure"} --artifact "$log"
  # A 429 or a dead token is not this task's problem alone: bench the worker for
  # every task until the cooldown expires.
  _maybe_cooldown "$dir" "$worker" "$rc" "$failure"
  if [[ -z "$sid" ]]; then
    echo "worker did not emit a resumable session id" >"$dir/resume-unavailable.txt"
  fi
  # The staged brief is a launch path, not part of the change: remove it before
  # anything reads the tree, so it can never reach a diff or a `git add -A`.
  rm -f "$wt/BRIEF.md"
  # Diff stat for supervisor
  local base
  base="$(cat "$dir/base-sha.txt" 2>/dev/null || true)"
  if [[ -n "$base" ]]; then
    git -C "$wt" diff --stat "$base" >"$dir/diff-stat.txt" 2>/dev/null || true
    _ssa_git_status_porcelain_uall "$wt" "$dir/wt-status.txt" "dispatch" 2>/dev/null || true
  fi
  # Quota snapshot after the run. This is the one place `|| true` on a network
  # call is right: a dead network must not turn a finished run into a failure.
  if [[ "${SSA_NO_QUOTA_SNAPSHOT:-}" != "1" && -f "$USAGE_PY" ]]; then
    python3 "$USAGE_PY" --json --fresh >"$dir/quota-after.json" 2>/dev/null || true
    [[ -s "$dir/quota-after.json" ]] || rm -f "$dir/quota-after.json"
  fi

  # Every worker leaves a compact final message, whether or not its CLI has an
  # output flag: the supervisor reads last-msg.txt, never stdout.log.
  _ssa_write_last_msg "$dir" "$worker" "$log"

  local resume="unavailable" argstr="defaults"
  [[ -n "$sid" ]] && resume="available"
  if [[ -s "$dir/worker-args.txt" ]]; then
    argstr="$(tr '\n' ' ' <"$dir/worker-args.txt" | sed 's/ *$//')"
  fi
  echo "smart-subagents: worker=$worker session=${sid:-unavailable}" \
    "resume=$resume exit=$rc" \
    "args=${argstr} log=$log"
  echo "  last-msg: $dir/last-msg.txt ($(wc -c <"$dir/last-msg.txt" 2>/dev/null | tr -d ' ' || echo 0) bytes)"
  _ssa_log_digest "$dir" "$worker" "$log" 3 400 | sed 's/^/  /'
  return "$rc"
}

# _ssa_write_last_msg DIR WORKER LOG: fill last-msg.txt from the log when the
# worker CLI did not write it itself (grok, kimi and claude have no -o flag).
_ssa_write_last_msg() {
  local dir="$1" worker="$2" log="$3" tmp
  [[ -s "$dir/last-msg.txt" ]] && return 0
  [[ -f "$log" ]] || return 0
  tmp="$(mktemp "${TMPDIR:-/tmp}/ssa-lastmsg.XXXXXX")"
  if _ssa final-message --worker "$worker" --log "$log" >"$tmp" 2>/dev/null \
      && [[ -s "$tmp" ]]; then
    mv "$tmp" "$dir/last-msg.txt"
  else
    rm -f "$tmp"
  fi
}

# _ssa_log_digest DIR WORKER LOG [MAX_EVENTS] [FINAL_CHARS]: a bounded view of
# the log. Never prints raw lines: one NDJSON line can be 100 KB+ and every
# byte printed here lands in the supervisor's context.
_ssa_log_digest() {
  local dir="$1" worker="$2" log="$3" events="${4:-6}" final="${5:-800}"
  [[ -f "$log" ]] || { echo "log: (none yet)"; return 0; }
  if [[ -n "$worker" && "$worker" != "-" ]] \
      && _ssa digest --worker "$worker" --log "$log" \
        --max-events "$events" --final-chars "$final" 2>/dev/null; then
    return 0
  fi
  # No registry entry for this worker: still never more than a few hundred bytes.
  echo "log: $(wc -c <"$log" | tr -d ' ') bytes, $(wc -l <"$log" | tr -d ' ') lines (unregistered worker, raw tail clipped)"
  tail -n "$events" "$log" | cut -c1-200 | sed 's/^/  | /'
}

# Copy the supervisor brief into the worktree so a file-ref worker can read
# it inside the sandbox. $dir/brief.md stays the supervisor artifact; the copy
# is a launch path only. Exclude it worktree-locally so git diff / scope never
# see it.
_ssa_stage_worktree_brief() {
  local wt="$1" src="$2"
  local dest="$wt/BRIEF.md" git_dir exclude common
  # --absolute-git-dir in a linked worktree is .git/worktrees/<id>, whose
  # info/exclude git never reads: the brief then showed up untracked in every
  # dispatch, which blocked cleanup and let `git add -A` commit it. The shared
  # info/exclude lives under the common dir, which may be relative to $wt.
  common="$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null || true)"
  [[ -n "$common" ]] || die "dispatch: cannot resolve git dir for $wt"
  git_dir="$(cd "$wt" && cd "$common" && pwd)" \
    || die "dispatch: cannot resolve git dir for $wt"
  exclude="$git_dir/info/exclude"
  mkdir -p "$(dirname "$exclude")"
  if ! grep -qxF '/BRIEF.md' "$exclude" 2>/dev/null; then
    printf '%s\n' '/BRIEF.md' >>"$exclude"
  fi
  cp "$src" "$dest" || die "dispatch: cannot copy brief into worktree"
  printf '%s' "$dest"
}

# --- background dispatch, watchdog, tail, stop --------------------------------
# A long run must not pin the supervisor session. The wrapper below detaches the
# worker into its own process group, records the pid plus its start time (so a
# recycled pid is never killed by mistake), and runs a watchdog that ends a run
# which has stopped writing logs and stopped touching the tree.

_pgid_of() {
  local pid="$1" v=""
  v="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  [[ -n "$v" ]] || v="$pid"
  printf '%s' "$v"
}

_kill_group() {
  # TERM the group, give it a grace period, then KILL. Falls back to the bare
  # pid when the process is not a group leader.
  local pgid="$1" grace="${SSA_KILL_GRACE_SECS:-10}" waited=0
  kill -TERM "-${pgid}" 2>/dev/null || kill -TERM "$pgid" 2>/dev/null || true
  while (( waited < grace )); do
    kill -0 "-${pgid}" 2>/dev/null || kill -0 "$pgid" 2>/dev/null || return 0
    sleep 1
    waited=$(( waited + 1 ))
  done
  kill -KILL "-${pgid}" 2>/dev/null || kill -KILL "$pgid" 2>/dev/null || true
}

_watchdog_should_exit() {
  # cmd_dispatch writes exit-code.txt when the worker returns. After that the
  # run is finished even if the bg-run wrapper is still alive (it waits on
  # this watchdog). Stalling then is 1788154673-70654 / 1788215189-74491:
  # reported -> stalled. Only THIS run's exit code counts: a leftover file from
  # an earlier dispatch used to disarm the watchdog on the first tick, which
  # left every re-dispatch unsupervised.
  local dir="$1" ec started
  [[ ! -f "$dir/stopped.txt" ]] || return 0
  [[ -f "$dir/exit-code.txt" ]] || return 1
  ec="$(_mtime "$dir/exit-code.txt")"
  started="$(_mtime "$dir/started-at.txt")"
  (( ec >= started ))
}

_watchdog() {
  # Stall detection is two signals, not one: a worker that is thinking still
  # writes to the log, and a worker that is editing still moves the tree.
  # TERM is ignored because the watchdog shares the wrapper's process group and
  # must outlive a TERM aimed at the run; cmd_bg_run reaps it with KILL after
  # dispatch. Do not trap KILL.
  local dir="$1" leader="$2" pgid=""
  local interval="${SSA_WATCHDOG_INTERVAL_SECS:-30}"
  local stall="${SSA_STALL_SECS:-600}"
  local deadline="${SSA_DEADLINE_SECS:-0}"
  local wt started idle=0 last_sz="" last_fp="" sz fp now reason=""
  wt="$(_read1 "$dir/wt.txt")"
  started="$(_now)"
  trap '' TERM
  while _pid_alive "$leader"; do
    sleep "$interval"
    _pid_alive "$leader" || break
    if _watchdog_should_exit "$dir"; then exit 0; fi
    sz="$(wc -c <"$dir/stdout.log" 2>/dev/null | tr -d ' ' || true)"
    [[ -n "$sz" ]] || sz=0
    fp=""
    if [[ -n "$wt" && -d "$wt" ]]; then
      fp="$(git -C "$wt" status --porcelain 2>/dev/null | cksum || true)"
    fi
    if [[ "$sz" == "$last_sz" && "$fp" == "$last_fp" ]]; then
      idle=$(( idle + interval ))
    else
      idle=0
    fi
    last_sz="$sz"; last_fp="$fp"
    if (( stall > 0 )) && (( idle >= stall )); then reason="stalled"; break; fi
    if (( deadline > 0 )); then
      now="$(_now)"
      if (( now - started >= deadline )); then reason="deadline"; break; fi
    fi
  done
  [[ -n "$reason" ]] || exit 0
  _pid_alive "$leader" || exit 0
  if _watchdog_should_exit "$dir"; then exit 0; fi
  {
    echo "$(_utc) $reason after ${idle}s idle (stall=${stall}s deadline=${deadline}s)"
  } >>"$dir/stalled.txt"
  _ssa_event "$dir" --phase stalled --failure-class "$reason" \
    --artifact "$dir/stalled.txt"
  _ssa_state "$dir" stalled --failure-class "$reason"
  # The worker's own group, written by cmd_dispatch. Killing the caller's group
  # instead took the bg-run wrapper with it, so a stalled run never produced an
  # exit code, a diff stat or a final message.
  pgid="$(_read1 "$dir/worker.pgid" "$(_read1 "$dir/worker.pid")")"
  [[ -n "$pgid" ]] || exit 0
  [[ "$pgid" != "$(_pgid_of "$$")" ]] || exit 0
  _kill_group "$pgid"
}

_dispatch_background() {
  local dir="$1" worker="$2" resume="${3:-}" pid="" waited=0
  local state resume_arg=()
  [[ -z "$resume" ]] || resume_arg=(--resume)
  state="$(_worker_state "$dir")"
  [[ "$state" != "running" ]] || \
    die "dispatch: a worker is already running for $dir (pid $(_read1 "$dir/worker.pid"))"
  # Every artifact of the previous run goes, not just the pid files: a stale
  # exit-code.txt disarms the watchdog, and a stale last-msg / diff-stat /
  # session id reads as this run's result.
  rm -f "$dir/worker.pid" "$dir/worker.pgid" "$dir/worker-start.txt" \
    "$dir/stalled.txt" "$dir/stopped.txt" "$dir/exit-code.txt" \
    "$dir/last-msg.txt" "$dir/diff-stat.txt" "$dir/wt-status.txt"
  # A resume needs the id it is resuming, so that one file survives.
  [[ -n "$resume" ]] || rm -f "$dir/session-id.txt" "$dir/resume-unavailable.txt"
  if command -v setsid >/dev/null 2>&1; then
    setsid bash "$SSA_SELF" bg-run --dir "$dir" --worker "$worker" \
      ${resume_arg[@]+"${resume_arg[@]}"} >>"$dir/bg.log" 2>&1 &
  else
    # bash 3.2 on macOS has no setsid: job control gives the child its own
    # process group, nohup keeps it alive past this shell.
    set -m
    nohup bash "$SSA_SELF" bg-run --dir "$dir" --worker "$worker" \
      ${resume_arg[@]+"${resume_arg[@]}"} >>"$dir/bg.log" 2>&1 &
    set +m
    disown 2>/dev/null || true
  fi
  # cmd_dispatch records the worker's own pid, which is the one worth killing.
  while (( waited < 200 )); do
    pid="$(_read1 "$dir/worker.pid")"
    [[ -z "$pid" ]] || break
    sleep 0.1
    waited=$(( waited + 1 ))
  done
  [[ -n "$pid" ]] || die "dispatch: background worker did not start (see $dir/bg.log)"
  echo "smart-subagents: background worker=$worker pid=$pid dir=$dir"
  echo "  status: bash \"$SSA_SELF\" status --dir \"$dir\"   # bounded digest, poll this"
  echo "  tail:   bash \"$SSA_SELF\" tail --dir \"$dir\"     # one line per event (--raw for the firehose)"
  echo "  stop:   bash \"$SSA_SELF\" stop --dir \"$dir\""
}

cmd_bg_run() {
  # Internal: the detached wrapper. Records identity, arms the watchdog, runs
  # the ordinary foreground dispatch, then reaps the watchdog.
  local dir="" worker="" rc=0 wd="" resume_arg=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      --worker) worker="${2:-}"; shift 2 ;;
      --resume) resume_arg=(--resume); shift ;;
      *) die "bg-run: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "bg-run: --dir required"
  # worker.pid is the WORKER's pid, written by cmd_dispatch once the job is up.
  # Recording this wrapper there made stop and the watchdog kill the wrapper,
  # so a killed run wrote no exit code, no diff stat and no final message.
  _utc >"$dir/started-at.txt"
  _watchdog "$dir" "$$" &
  wd=$!
  cmd_dispatch --dir "$dir" --worker "$worker" \
    ${resume_arg[@]+"${resume_arg[@]}"} || rc=$?
  # The watchdog traps TERM (it must outlive a TERM aimed at this run). TERM
  # therefore never reaps it, and wait deadlocks until the stall timer fires on
  # an already-exited task. KILL is the only signal it cannot ignore.
  kill -KILL "$wd" 2>/dev/null || true
  wait "$wd" 2>/dev/null || true
  return "$rc"
}

cmd_tail() {
  # Follows the log as one short line per event (tool calls, result sizes,
  # assistant text clipped). --raw is the unfiltered firehose: a streaming
  # worker emits 100 KB lines, so only use it when redirecting to a file.
  local dir="" raw="" lines=20
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      --raw) raw=1; shift ;;
      -n|--lines) lines="${2:-20}"; shift 2 ;;
      *) die "tail: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "tail: --dir required"
  [[ -f "$dir/stdout.log" ]] || die "tail: no stdout.log yet in $dir"
  if [[ -n "$raw" ]]; then
    exec tail -n "$lines" -f "$dir/stdout.log"
  fi
  tail -n "$lines" -f "$dir/stdout.log" | _ssa tail-filter
}

cmd_stop() {
  local dir=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      *) die "stop: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "stop: --dir required"
  local pid pgid state
  pid="$(_read1 "$dir/worker.pid")"
  state="$(_worker_state "$dir")"
  if [[ -z "$pid" ]]; then
    echo "stop: nothing to stop, no worker.pid in $dir (phase=$(_task_phase "$dir"))"
    return 1
  fi
  case "$state" in
    running) ;;
    reused)
      echo "stop: refusing, pid $pid is alive but its start time does not match" \
        "the recorded one, so it is a different process now"
      return 1 ;;
    *)
      echo "stop: worker $pid is not running (state=$state," \
        "exit=$(_read1 "$dir/exit-code.txt" '?'))"
      return 1 ;;
  esac
  pgid="$(_read1 "$dir/worker.pgid" "$pid")"
  _kill_group "$pgid"
  echo "$(_utc) stopped by operator (pid $pid, pgid $pgid)" >>"$dir/stopped.txt"
  _ssa_event "$dir" --phase aborted --pid "$pid" --artifact "$dir/stopped.txt"
  _ssa_state "$dir" aborted
  echo "stop: signalled process group $pgid for $dir"
}

# --- ls / status: what is on this machine right now ---------------------------

cmd_ls() {
  # A machine that has been dispatching for a month holds a hundred task dirs,
  # and printing all of them is 12 KB of context for the two that are live.
  # Default: the recent ones plus everything still in flight.
  local all="" want_state="" limit="${SSA_LS_LIMIT:-20}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all) all=1; shift ;;
      --state) want_state="${2:-}"; shift 2 ;;
      *) die "ls: unknown arg $1" ;;
    esac
  done
  [[ -d "$SSA_WORK_DIR" ]] || { echo "no task dirs under $SSA_WORK_DIR"; return 0; }
  local now d id age repo worker class phase state diff hidden=0 shown=0
  local tmp live
  now="$(_now)"
  tmp="$(mktemp "${TMPDIR:-/tmp}/ssa-ls.XXXXXX")"
  for d in "$SSA_WORK_DIR"/*; do
    [[ -d "$d" ]] || continue
    id="$(basename "$d")"
    [[ "$id" != "wt" ]] || continue
    age="$(_age_human $(( now - $(_mtime "$d") )) )"
    repo="$(basename "$(_read1 "$d/repo.txt" '?')")"
    if [[ "$id" == plan-* ]]; then
      local plans
      plans="$(find "$d" -maxdepth 1 -name 'plan-*.md' 2>/dev/null | wc -l | tr -d ' ')"
      state="panel"
      live=0
      [[ -f "$d/panel-done.txt" ]] || live=1
      printf '%s\t%s\t%s\n' "$(_mtime "$d")" "$live:$state" \
        "$(printf '%-18s %5s %-16s %-7s %-22s %-9s %-12s %s' \
          "$id" "$age" "$repo" "panel" "$(_task_class "$d")" "panel" "-" \
          "${plans} plan(s)")" >>"$tmp"
      continue
    fi
    [[ -f "$d/task-id.txt" ]] || continue
    worker="$(_read1 "$d/worker.txt" '-')"
    class="$(_task_class "$d")"
    phase="$(_task_phase "$d")"
    state="$(_task_state "$d")"
    diff="$(_diff_summary "$d")"
    [[ ! -f "$d/stalled.txt" ]] || phase="${phase}!"
    # In flight: still worth showing however old the dir is.
    live=0
    case "$state" in running|picked|briefed) live=1 ;; esac
    case "$phase" in running|briefed) live=1 ;; esac
    printf '%s\t%s\t%s\n' "$(_mtime "$d")" "$live:$state" \
      "$(printf '%-18s %5s %-16s %-7s %-22s %-9s %-12s %s' \
        "$id" "$age" "$repo" "$worker" "$class" "$phase" "$state" "$diff")" >>"$tmp"
  done
  printf '%-18s %5s %-16s %-7s %-22s %-9s %-12s %s\n' \
    TASK AGE REPO WORKER SIZE/DIFF/KIND PHASE STATE DIFF
  local key row
  while IFS=$'\t' read -r _ key row; do
    if [[ -n "$want_state" && "${key#*:}" != "$want_state" ]]; then
      continue
    fi
    if [[ -z "$all" ]] && (( shown >= limit )) && [[ "${key%%:*}" != "1" ]]; then
      hidden=$(( hidden + 1 ))
      continue
    fi
    printf '%s\n' "$row"
    shown=$(( shown + 1 ))
  done < <(sort -rn -k1,1 "$tmp")
  rm -f "$tmp"
  (( shown > 0 )) || echo "(none)"
  (( hidden == 0 )) || echo "… $hidden older tasks hidden (--all)"
}

cmd_status() {
  local dir=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      *) die "status: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "status: --dir required"
  local now wt sid state pid
  now="$(_now)"
  wt="$(_read1 "$dir/wt.txt" '-')"
  sid="$(_read1 "$dir/session-id.txt")"
  state="$(_worker_state "$dir")"
  pid="$(_read1 "$dir/worker.pid" '-')"
  echo "task      : $(_read1 "$dir/task-id.txt" "$(basename "$dir")")"
  echo "dir       : $dir"
  echo "age       : $(_age_human $(( now - $(_mtime "$dir") )) )"
  echo "repo      : $(_read1 "$dir/repo.txt" '?')"
  echo "worktree  : $wt"
  echo "worker    : $(_read1 "$dir/worker.txt" '-')"
  echo "class     : $(_task_class "$dir")"
  echo "phase     : $(_task_phase "$dir")"
  # phase is inferred from which artifacts exist; state is what the task
  # recorded about itself. They disagree exactly when something went wrong.
  echo "state     : $(_task_state "$dir")$(
    [[ -f "$dir/task.json" ]] || printf ' (inferred, no task.json)'
  )"
  if [[ -f "$dir/events.jsonl" ]]; then
    echo "events    : $(wc -l <"$dir/events.jsonl" | tr -d ' ') recorded"
  fi
  echo "base sha  : $(_read1 "$dir/base-sha.txt" '-')"
  if [[ -d "$wt" ]]; then
    echo "branch    : $(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  else
    echo "branch    : -"
  fi
  echo "exit code : $(_read1 "$dir/exit-code.txt" '-')"
  if [[ -n "$sid" ]]; then
    echo "session   : $sid"
  else
    echo "session   : resume=unavailable"
  fi
  echo "worker pid: $pid ($state)"
  [[ ! -f "$dir/stalled.txt" ]] || echo "stalled   : $(tail -n1 "$dir/stalled.txt")"
  [[ ! -f "$dir/stopped.txt" ]] || echo "stopped   : $(tail -n1 "$dir/stopped.txt")"
  echo "diff      : $(_diff_summary "$dir")"
  if [[ -f "$dir/outcome.json" ]]; then
    echo "verify    : $(python3 - "$dir/outcome.json" <<'PY'
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception as exc:
    print("unreadable outcome.json: %s" % exc)
else:
    v = doc.get("verify") or {}
    print("verdict=%s new_failures=%s scope_ok=%s secrets_ok=%s" % (
        v.get("verdict"), v.get("new_failures"), v.get("scope_ok"),
        v.get("secrets_ok")))
PY
)"
  else
    echo "verify    : not run (bash \"$SSA_SELF\" verify --dir \"$dir\")"
  fi
  if [[ -f "$dir/outcome-record.json" ]]; then
    echo "recorded  : yes"
  else
    echo "recorded  : no (bash \"$SSA_SELF\" record --dir \"$dir\" --outcome ...)"
  fi
  if [[ -f "$dir/stdout.log" ]]; then
    echo "log digest:"
    _ssa_log_digest "$dir" "$(_read1 "$dir/worker.txt" '-')" "$dir/stdout.log" 4 600 \
      | sed 's/^/  /'
  else
    echo "log digest: (no stdout.log)"
  fi
}

# --- plan: parallel planning panel -------------------------------------------
# Fans a goal out to N planners with deliberately different lenses, spread
# across whichever CLIs have quota, and collects their plans for a supervisor
# to consolidate. Read-only: planners never write to the tree.

# Lenses are the point. Three planners that all reason the same way are one
# planner with extra latency; the value is in disagreement the supervisor
# has to reconcile.
_lens_name() {
  case "$1" in
    0) echo "pragmatic" ;;
    1) echo "risk" ;;
    2) echo "architecture" ;;
    3) echo "constraints" ;;
    *) echo "alt$1" ;;
  esac
}

_lens_prompt() {
  case "$1" in
    pragmatic)
      echo "Optimize for the smallest correct change that ships. Prefer the least
invasive edit that satisfies the goal. Call out anything in the goal that is
scope creep and should be dropped or deferred." ;;
    risk)
      echo "Optimize for what breaks. Lead with the failure modes: what regresses,
what has no test coverage, what is hard to roll back, what breaks for existing
data or callers. Name the single riskiest step and how to de-risk it." ;;
    architecture)
      echo "Optimize for the right long-term shape. Identify what is load-bearing,
what is already the wrong abstraction, and which refactor makes this change and
the next three easier. Say explicitly what you would NOT refactor now." ;;
    constraints)
      echo "Optimize for what the repo already decided. Extract the conventions,
contracts and invariants this change must respect from the code and its agent
docs, and plan strictly within them." ;;
    *)
      echo "Plan this change on its merits." ;;
  esac
}

_PLAN_REPO=""
_PLAN_WT=""

_plan_rollback() {
  local rc=$?
  [[ -n "$_PLAN_WT" ]] || exit "$rc"
  # Read-only scratch: nothing in a panel worktree is worth keeping, and a
  # leftover detached worktree is what gc would later delete under a live run.
  git -C "$_PLAN_REPO" worktree remove --force "$_PLAN_WT" >/dev/null 2>&1 \
    || rm -rf "$_PLAN_WT"
  git -C "$_PLAN_REPO" worktree prune >/dev/null 2>&1 || true
  echo "smart-subagents: plan failed, removed the shared worktree $_PLAN_WT" >&2
  exit "$rc"
}

cmd_plan() {
  local repo="" goal="" goal_file="" n=3 difficulty="hard" size="medium" kind="default"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo) repo="${2:-}"; shift 2 ;;
      --goal) goal="${2:-}"; shift 2 ;;
      --goal-file) goal_file="${2:-}"; shift 2 ;;
      --n) n="${2:-}"; shift 2 ;;
      --difficulty) difficulty="${2:-}"; shift 2 ;;
      --size) size="${2:-}"; shift 2 ;;
      --kind) kind="${2:-}"; shift 2 ;;
      *) die "plan: unknown arg $1" ;;
    esac
  done
  [[ -n "$repo" && -d "$repo" ]] || die "plan: --repo required (absolute path)"
  if [[ -n "$goal_file" ]]; then
    [[ -f "$goal_file" ]] || die "plan: --goal-file not found: $goal_file"
    goal="$(cat "$goal_file")"
  fi
  [[ -n "$goal" ]] || die "plan: --goal or --goal-file required"
  [[ "$n" =~ ^[0-9]+$ ]] && (( n >= 1 )) || die "plan: --n must be a positive integer"
  need git; need python3

  local task_id dir
  task_id="$(date +%s)-$$"
  _ssa_ensure_work_dir
  dir="${SSA_WORK_DIR}/plan-${task_id}"
  mkdir -m 700 -p "$dir"
  printf '%s\n' "$goal" >"$dir/goal.md"
  echo "$repo" >"$dir/repo.txt"
  echo "$kind" >"$dir/kind.txt"

  python3 "$USAGE_PY" --json --task-size "$size" --difficulty "$difficulty" \
    --task-kind "$kind" \
    >"$dir/usage.json" || die "plan: usage preflight failed"

  # Eligible workers, best headroom first. Planners round-robin across them, so
  # a single-CLI day still gets N independent plans rather than zero.
  local workers
  workers="$(python3 - "$dir/usage.json" <<'PY'
import json,sys
rec=json.load(open(sys.argv[1]))["recommendation"]
print(" ".join(r["cli"] for r in rec.get("ranked") or []))
for cli, args in (rec.get("worker_args") or {}).items():
    with open(sys.argv[1].rsplit("/", 1)[0] + "/worker-args-" + cli + ".txt", "w") as fh:
        fh.write("\n".join(args) + ("\n" if args else ""))
PY
)"
  [[ -n "$workers" ]] || die "plan: no eligible worker CLI (all exhausted); see $dir/usage.json"
  # shellcheck disable=SC2206
  local warr=($workers)

  # One shared read-only worktree: planners read a pinned tree, never the user's.
  # It sits beside the panel dir, not inside it, so a planner's cwd cannot
  # reach the panel's own artifacts.
  local wt
  wt="$(_ssa_wt_path "plan-${task_id}")"
  mkdir -m 700 -p "$(dirname "$wt")"
  git -C "$repo" worktree add --detach "$wt" >/dev/null 2>&1 \
    || die "plan: worktree add failed for $repo"
  echo "$wt" >"$dir/wt.txt"
  _PLAN_REPO="$repo"; _PLAN_WT="$wt"
  trap '_plan_rollback' EXIT

  local i pids=() lenses=()
  for (( i=0; i<n; i++ )); do
    local lens worker brief plan_out
    lens="$(_lens_name "$i")"
    worker="${warr[$(( i % ${#warr[@]} ))]}"
    lenses+=("$lens:$worker")
    brief="$dir/brief-$i-$lens.md"
    plan_out="$dir/plan-$i-$lens-$worker.md"
    {
      echo "# Planning task"
      echo
      echo "## Goal"
      echo
      cat "$dir/goal.md"
      echo
      echo "## Your lens"
      echo
      _lens_prompt "$lens"
      echo
      echo "## Rules"
      echo
      echo "- Read the repository at $wt. This is a planning task: change NOTHING."
      echo "- If the repo has an agent contract (AGENTS.md, CLAUDE.md, CONTRIBUTING.md,"
      echo "  or a domain rules doc), read it first and plan within it."
      echo "- Cite concrete file:line for every claim about existing code."
      echo "- Do not invent files or APIs. If you are unsure something exists, say so."
      echo "- ast-grep is for syntactic search. The cgc graph does not cover this worktree."
      echo "- On a structural question, pack from the indexed repo or follow a printed miss fallback. Do not grep-fan-out."
      echo
      echo "## Output"
      echo
      echo "Markdown, no preamble, in this order:"
      echo "1. One-paragraph approach summary."
      echo "2. Ordered steps. Each: what changes, which files, how it is verified."
      echo "3. Risks and unknowns, worst first."
      echo "4. Open questions a human must answer. Empty list is a valid answer."
    } >"$brief"

    (
      local args_file="$dir/worker-args-$worker.txt"
      [[ -f "$args_file" ]] || : >"$args_file"
      if ! _ssa_build "$worker" plan --worktree "$wt" --brief "$brief" \
          --output "$plan_out" --args-file "$args_file" \
          || [[ -z "$_BC_BIN" || ! -x "$_BC_BIN" ]]; then
        echo "(planner produced no output; $worker binary unavailable)" >"$plan_out"
        exit 0
      fi
      # Where the plan text lands is a registry fact too: codex writes it with
      # a flag, the others write it on stdout.
      if [[ "$_BC_OUTPUT_MODE" == "stdout" ]]; then
        if [[ -n "$_BC_STDIN" ]]; then
          _ssa_run_worker <"$_BC_STDIN" >"$plan_out" 2>"$dir/plan-$i.log" || true
        else
          _ssa_run_worker >"$plan_out" 2>"$dir/plan-$i.log" || true
        fi
      else
        if [[ -n "$_BC_STDIN" ]]; then
          _ssa_run_worker <"$_BC_STDIN" >"$dir/plan-$i.log" 2>&1 || true
        else
          _ssa_run_worker >"$dir/plan-$i.log" 2>&1 || true
        fi
      fi
    ) &
    pids+=($!)
  done

  local pid
  for pid in "${pids[@]}"; do wait "$pid" || true; done

  # The fallback lives out here: a planner that died mid-subshell still owes
  # the panel a plan file, and "see plan-N.log" pointed the supervisor at a
  # raw NDJSON log measured at 555 KB. A digest is the readable half of it.
  local lens_worker
  for (( i=0; i<n; i++ )); do
    lens_worker="${lenses[$i]}"
    lens="${lens_worker%%:*}"
    worker="${lens_worker#*:}"
    plan_out="$dir/plan-$i-$lens-$worker.md"
    [[ ! -s "$plan_out" ]] || continue
    {
      echo "(planner produced no output. Digest of $dir/plan-$i.log follows.)"
      echo
      _ssa digest --worker "$worker" --log "$dir/plan-$i.log" \
        --max-events 10 --final-chars 2000 2>/dev/null \
        || echo "(no digest: $worker is not registered, or the log is empty)"
    } >"$plan_out"
  done

  # C3: planners are dispatched read-only, but grok's sandbox is `workspace`
  # and nothing checked afterwards. Say so rather than assume.
  local dirty=false
  if [[ -n "$(git -C "$wt" status --porcelain 2>/dev/null || true)" ]]; then
    dirty=true
    git -C "$wt" status --porcelain >"$dir/panel-dirty.txt" 2>/dev/null || true
    echo "smart-subagents: plan: planners left the shared worktree dirty, see $dir/panel-dirty.txt" >&2
  fi
  # gc reads this: plan-*.md files exist from the moment a redirect opens, so
  # they never meant the panel was finished.
  _utc >"$dir/panel-done.txt"

  python3 - "$dir" "$SSA_CLI_PY" "$dirty" "${lenses[@]}" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
cli_py, dirty = sys.argv[2], sys.argv[3] == "true"
plans = []
for f in sorted(d.glob("plan-*-*.md")):
    text = f.read_text(errors="replace").strip()
    parts = f.stem.split("-")
    index, lens, worker = parts[1], parts[2], parts[3]
    log = d / ("plan-%s.log" % index)
    entry = {"file": str(f), "lens": lens, "worker": worker, "bytes": len(text),
             "empty": text.startswith("(planner produced no output")}
    if log.exists():
        # A byte count and a ready command, never the path on its own: the log
        # is NDJSON nobody should cat.
        entry["log_bytes"] = log.stat().st_size
        entry["log_digest_cmd"] = (
            'python3 %s digest --worker %s --log %s --max-events 30 --final-chars 4000'
            % (cli_py, worker, log)
        )
    plans.append(entry)
wt = (d / "wt.txt").read_text().strip() if (d / "wt.txt").exists() else ""
doc = {"dir": str(d), "worktree": wt,
       "goal": str(d / "goal.md"), "planners": sys.argv[4:],
       "plans": plans,
       "next": "supervisor: read every plan, reconcile disagreements, "
               "emit one plan"}
if dirty:
    doc["dirty"] = True
    doc["dirty_report"] = str(d / "panel-dirty.txt")
print(json.dumps(doc, indent=2))
PY
  trap - EXIT
  _PLAN_WT=""; _PLAN_REPO=""
}

cmd_scan_secrets() {
  local dir=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      *) die "scan-secrets: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "scan-secrets: --dir required"
  local wt base findings diff_file added_file names_file scan_tmp gitleaks_report
  wt="$(cat "$dir/wt.txt" 2>/dev/null || true)"
  base="$(cat "$dir/base-sha.txt" 2>/dev/null || true)"
  [[ -n "$wt" && -d "$wt" ]] || die "scan-secrets: missing worktree ($dir/wt.txt)"
  [[ -n "$base" ]] || die "scan-secrets: missing base SHA ($dir/base-sha.txt)"
  findings="$dir/verify-secrets.txt"
  diff_file="$dir/verify-secrets.diff"
  added_file="$dir/verify-secrets-added.txt"
  names_file="$dir/verify-secrets-names.txt"
  local untracked_file="$dir/verify-secrets-untracked.z"
  : >"$findings"
  git -C "$wt" diff --no-ext-diff --unified=0 "$base" -- >"$diff_file"
  git -C "$wt" diff --name-status --diff-filter=A "$base" -- >"$names_file"
  # A worker that leaks a credential leaks it in a file it never staged, so the
  # tracked diff alone cannot see it. Untracked files count as added lines.
  git -C "$wt" status --porcelain -uall -z >"$untracked_file" 2>/dev/null || : >"$untracked_file"
  python3 - "$diff_file" "$added_file" "$names_file" "$findings" "$wt" "$untracked_file" <<'PY'
import math
import os
import re
import sys
from collections import Counter

diff_path, added_path, names_path, findings_path, wt, untracked_path = sys.argv[1:]
patterns = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{20,}")),
    ("slack-token", re.compile(r"xox[abpsr]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("private-key-header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
token_re = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
env_re = re.compile(r"(^|/)\.env(\..+)?$")
findings = []

def masked(value):
    return value[:4] + "*" * max(4, len(value) - 4)

def entropy(value):
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log(count / length, 2)
                for count in counts.values())

def untracked_paths():
    """`?? path` entries from a NUL-separated porcelain status."""
    try:
        with open(untracked_path, "rb") as fh:
            blob = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for entry in blob.split("\0"):
        if entry.startswith("?? "):
            out.append(entry[3:])
    return out


def readable_lines(path):
    """Text lines of a file small enough to scan. Binaries are skipped."""
    full = os.path.join(wt, path)
    try:
        if os.path.getsize(full) > 1_000_000:
            return []
        with open(full, "rb") as fh:
            blob = fh.read()
    except OSError:
        return []
    if b"\0" in blob:
        return []
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return text.splitlines()


for raw in open(names_path, errors="replace"):
    fields = raw.rstrip("\n").split("\t", 1)
    if len(fields) == 2 and env_re.search(fields[1]):
        findings.append("env-file: " + fields[1])

new_files = []
for path in untracked_paths():
    if env_re.search(path):
        findings.append("env-file: " + path)
    new_files.append(path)

def added_lines():
    for raw in open(diff_path, errors="replace"):
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        yield raw[1:].rstrip("\n")
    for path in new_files:
        for line in readable_lines(path):
            yield line


with open(added_path, "w") as added:
    for line in added_lines():
        added.write(line + "\n")
        matches = []
        names = []
        for name, pattern in patterns:
            found = list(pattern.finditer(line))
            if found:
                names.append(name)
                matches.extend(found)
        # 3.5, not 4.5: a 40-char hex token (a git sha, an API token minted as
        # hex) measures 3.84, so the old threshold never saw one.
        entropy_matches = [
            match for match in token_re.finditer(line)
            if entropy(match.group(0)) > 3.5
        ]
        if entropy_matches:
            names.append("high-entropy-token")
            matches.extend(entropy_matches)
        if not matches:
            continue
        spans = []
        for start, end in sorted({(match.start(), match.end()) for match in matches}):
            if spans and start <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))
        masked_line = []
        position = 0
        for start, end in spans:
            masked_line.append(line[position:start])
            masked_line.append(masked(line[start:end]))
            position = end
        masked_line.append(line[position:])
        safe_line = "".join(masked_line)
        for name in dict.fromkeys(names):
            findings.append(name + ": " + safe_line)

with open(findings_path, "a") as out:
    for finding in findings:
        out.write(finding + "\n")
PY

  echo absent >"$dir/verify-secrets-gitleaks.txt"
  if command -v gitleaks >/dev/null 2>&1; then
    echo ran >"$dir/verify-secrets-gitleaks.txt"
    scan_tmp="$(mktemp -d "${TMPDIR:-/tmp}/ssa-gitleaks.XXXXXX")"
    gitleaks_report="$scan_tmp/report.json"
    mkdir "$scan_tmp/source"
    cp "$added_file" "$scan_tmp/source/added-lines.txt"
    if ! gitleaks detect --no-git --source "$scan_tmp/source" \
        --report-format json --report-path "$gitleaks_report" >/dev/null 2>&1; then
      python3 - "$gitleaks_report" "$findings" <<'PY'
import json
import sys
try:
    records = json.load(open(sys.argv[1]))
except Exception:
    records = []
with open(sys.argv[2], "a") as out:
    for record in records:
        secret = str(record.get("Secret") or "")
        masked = secret[:4] + "*" * max(4, len(secret) - 4)
        rule = record.get("RuleID") or "finding"
        out.write("gitleaks-{}: {}\n".format(rule, masked))
PY
    fi
    rm -rf "$scan_tmp"
  fi
  rm -f "$diff_file" "$added_file" "$names_file" "$untracked_file"
  [[ ! -s "$findings" ]]
}

cmd_diff() {
  # `git diff` on a worker's change is unbounded, and the supervisor reads it
  # into a context. Default is the stat; a path drills down, clipped.
  local dir="" path="" max=20000
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      --path) path="${2:-}"; shift 2 ;;
      --max-bytes) max="${2:-}"; shift 2 ;;
      *) die "diff: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "diff: --dir required"
  [[ "$max" =~ ^[0-9]+$ ]] || die "diff: --max-bytes takes a whole number"
  local wt base total
  wt="$(_read1 "$dir/wt.txt")"
  base="$(_read1 "$dir/base-sha.txt")"
  [[ -n "$wt" && -d "$wt" ]] || die "diff: missing worktree ($dir/wt.txt)"
  [[ -n "$base" ]] || die "diff: missing base SHA ($dir/base-sha.txt)"
  echo "### diff stat"
  git -C "$wt" diff --stat "$base" 2>/dev/null || true
  [[ -n "$path" ]] || {
    echo "(per-file: diff --dir DIR --path PATH [--max-bytes N])"
    return 0
  }
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/ssa-diff.XXXXXX")"
  git -C "$wt" diff "$base" -- "$path" >"$tmp" 2>/dev/null || true
  total="$(wc -c <"$tmp" | tr -d ' ')"
  printf '### diff %s\n' "$path"
  head -c "$max" "$tmp"
  if (( total > max )); then
    printf '\n(truncated, %s bytes total)\n' "$total"
  fi
  rm -f "$tmp"
}

cmd_verify_summary() {
  local dir="" base=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      *) die "verify-summary: unknown $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "verify-summary: --dir required"
  local wt base full sec
  wt="$(cat "$dir/wt.txt")"
  base="$(cat "$dir/base-sha.txt")"
  # Measured 203 KB on a real repo, every byte of it into a supervisor context.
  # Each section is clipped here and written in full to one file on disk.
  full="$dir/verify-summary-full.txt"
  : >"$full"
  sec="$(mktemp "${TMPDIR:-/tmp}/ssa-summary.XXXXXX")"
  echo "### branch"; git -C "$wt" branch --show-current
  if _ssa_git_status_porcelain_uall "$wt" "$sec" "verify-summary"; then
    _ssa_clip_file "$sec" status 200 "$full"
  else
    echo "### status"
    echo "(unavailable: git status -uall is unusable in $wt)"
  fi
  git -C "$wt" log --oneline "${base}..HEAD" >"$sec" 2>/dev/null || : >"$sec"
  _ssa_clip_file "$sec" "commits since base" 200 "$full"
  git -C "$wt" diff --stat "$base" >"$sec" 2>/dev/null || : >"$sec"
  _ssa_clip_file "$sec" "diff stat" 200 "$full"
  git -C "$wt" diff --name-status "$base" >"$sec" 2>/dev/null || : >"$sec"
  _ssa_clip_file "$sec" "name-status" 200 "$full"
  rm -f "$sec"
  echo "### secrets"
  if cmd_scan_secrets --dir "$dir"; then
    echo "PASS"
  else
    echo "FAIL: $dir/verify-secrets.txt"
    return 1
  fi
}

# --- cleanup / gc: retire worktrees without losing work -----------------------
# Deleting a task dir throws away a worktree, a branch and every log. So the
# default is to refuse and explain, and only an explicit --force overrides.

_task_unsafe_reason() {
  # Prints the reason a task dir must not be deleted, empty when it is safe.
  local dir="$1" wt base state cnt st
  state="$(_worker_state "$dir")"
  # "reused" means the pid belongs to somebody else now, which is the opposite
  # of a reason to keep the dir: nothing of ours is running.
  if [[ "$state" == "running" ]]; then
    printf 'worker process %s is alive' "$(_read1 "$dir/worker.pid" '?')"
    return 0
  fi
  wt="$(_read1 "$dir/wt.txt")"
  if [[ -n "$wt" && "$wt" != "NOT_GIT" && -d "$wt" ]]; then
    st="$(mktemp "${TMPDIR:-/tmp}/ssa-unsafe-status.XXXXXX")"
    if ! _ssa_git_status_porcelain_uall "$wt" "$st" "cleanup" 2>/dev/null; then
      rm -f "$st"
      printf 'worktree status is unreadable'
      return 0
    fi
    # A staged brief is a launch artifact, not the worker's work.
    if [[ -n "$(grep -v '^?? BRIEF.md$' "$st" || true)" ]]; then
      rm -f "$st"
      printf 'worktree has uncommitted changes'
      return 0
    fi
    rm -f "$st"
    base="$(_read1 "$dir/base-sha.txt")"
    if [[ -n "$base" ]]; then
      cnt="$(git -C "$wt" rev-list --count "${base}..HEAD" 2>/dev/null || echo 0)"
      if [[ "$cnt" != "0" ]]; then
        printf 'branch has %s commit(s) not reachable from base' "$cnt"
        return 0
      fi
    fi
  fi
  printf ''
}

_cleanup_apply() {
  local dir="$1" force="${2:-}" repo wt id
  repo="$(_read1 "$dir/repo.txt")"
  wt="$(_read1 "$dir/wt.txt")"
  id="$(_read1 "$dir/task-id.txt" "$(basename "$dir")")"
  if [[ -n "$repo" && -d "$repo" && -n "$wt" && "$wt" != "NOT_GIT" ]]; then
    if [[ -n "$force" ]]; then
      git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"
    else
      git -C "$repo" worktree remove "$wt" >/dev/null 2>&1 || rm -rf "$wt"
    fi
    git -C "$repo" branch -D "ssa/${id}" >/dev/null 2>&1 || true
    git -C "$repo" worktree prune >/dev/null 2>&1 || true
    # The worktree is a sibling of the task dir now, so removing the dir is not
    # enough; and the shared parent goes when it empties.
    rm -rf "$wt"
    rmdir "$SSA_WORK_DIR/wt" >/dev/null 2>&1 || true
  fi
  rm -rf "$dir"
}

cmd_cleanup() {
  local dir="" force=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      --force) force=1; shift ;;
      *) die "cleanup: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "cleanup: --dir required"
  local reason
  reason="$(_task_unsafe_reason "$dir")"
  if [[ -n "$reason" && -z "$force" ]]; then
    echo "cleanup: refused for $dir: $reason (use --force to delete anyway)"
    return 1
  fi
  _cleanup_apply "$dir" "$force"
  if [[ -n "$reason" ]]; then
    echo "cleanup: forced removal of $dir despite: $reason"
  else
    echo "cleanup: removed worktree, ssa branch and task dir $dir"
  fi
}

cmd_gc() {
  local days=7 dry=1 verbose=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --older-than) days="${2:-}"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      --no-dry-run) dry=0; shift ;;
      --verbose) verbose=1; shift ;;
      *) die "gc: unknown arg $1" ;;
    esac
  done
  [[ "$days" =~ ^[0-9]+$ ]] || die "gc: --older-than takes whole days"
  [[ -d "$SSA_WORK_DIR" ]] || { echo "gc: nothing under $SSA_WORK_DIR"; return 0; }
  local now cutoff d age reason kind plans deleted=0 kept=0
  # Kept rows are the boring majority (171 of them on a real machine), so they
  # are counted by reason instead of printed one per dir. --verbose restores
  # the per-dir lines.
  local k_young=0 k_alive=0 k_dirty=0 k_commits=0 k_other=0 summary=""
  now="$(_now)"
  cutoff=$(( days * 86400 ))
  for d in "$SSA_WORK_DIR"/*; do
    [[ -d "$d" ]] || continue
    [[ "$(basename "$d")" != "wt" ]] || continue
    age=$(( now - $(_mtime "$d") ))
    reason="$(_task_unsafe_reason "$d")"
    kind="task"
    if [[ "$(basename "$d")" == plan-* ]]; then
      kind="panel"
      plans="$(find "$d" -maxdepth 1 -name 'plan-*.md' 2>/dev/null | wc -l | tr -d ' ')"
      # A finished panel is pure read-only scratch: its worktree is detached and
      # its value is the plan files a supervisor already consumed. "Finished"
      # is panel-done.txt, not the presence of plan files: those exist from the
      # moment the redirect opens, so gc used to delete a live panel's worktree
      # out from under its planners.
      if [[ -z "$reason" && "$plans" != "0" && -f "$d/panel-done.txt" ]]; then
        age="$cutoff"
      elif [[ -z "$reason" && ! -f "$d/panel-done.txt" ]]; then
        reason="panel still running (no panel-done.txt)"
      fi
    fi
    if [[ -z "$reason" ]] && (( age < cutoff )); then
      reason="younger than ${days}d"
    fi
    if [[ -n "$reason" ]]; then
      kept=$(( kept + 1 ))
      case "$reason" in
        "younger than "*) k_young=$(( k_young + 1 )) ;;
        "worker process "*) k_alive=$(( k_alive + 1 )) ;;
        "worktree has uncommitted changes") k_dirty=$(( k_dirty + 1 )) ;;
        "branch has "*) k_commits=$(( k_commits + 1 )) ;;
        *) k_other=$(( k_other + 1 )) ;;
      esac
      [[ -z "$verbose" ]] || \
        echo "kept  $kind $(basename "$d") ($(_age_human "$age")): $reason"
      continue
    fi
    if (( dry == 1 )); then
      echo "safe  $kind $(basename "$d") ($(_age_human "$age")): would delete"
    else
      _cleanup_apply "$d" ""
      echo "gone  $kind $(basename "$d") ($(_age_human "$age")): deleted"
    fi
    deleted=$(( deleted + 1 ))
  done
  if (( kept > 0 )) && [[ -z "$verbose" ]]; then
    (( k_young == 0 )) || summary="${summary}${summary:+, }$k_young younger than ${days}d"
    (( k_alive == 0 )) || summary="${summary}${summary:+, }$k_alive worker alive"
    (( k_dirty == 0 )) || summary="${summary}${summary:+, }$k_dirty dirty worktree"
    (( k_commits == 0 )) || summary="${summary}${summary:+, }$k_commits unmerged commits"
    (( k_other == 0 )) || summary="${summary}${summary:+, }$k_other other"
    echo "kept: $summary (--verbose for one line per dir)"
  fi
  if (( dry == 1 )); then
    echo "gc: dry run, $deleted safe, $kept kept (pass --no-dry-run to delete)"
  else
    echo "gc: deleted $deleted, kept $kept"
  fi
}

# --- doctor: is this machine able to dispatch at all? -------------------------

_doc_row() {
  # state<TAB>check<TAB>detail, collected then rendered once.
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" >>"$_DOC_FILE"
  [[ "$1" != "fail" ]] || _DOC_FATAL=$(( _DOC_FATAL + 1 ))
}

_DOC_FILE=""
_DOC_FATAL=0

cmd_doctor() {
  local json=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) json=1; shift ;;
      *) die "doctor: unknown arg $1" ;;
    esac
  done
  _DOC_FILE="$(mktemp "${TMPDIR:-/tmp}/ssa-doctor.XXXXXX")"
  _DOC_FATAL=0

  if command -v python3 >/dev/null 2>&1; then
    _doc_row ok python3 "$(python3 -V 2>&1 | head -n1)"
  else
    _doc_row fail python3 "not on PATH, no command can run"
  fi
  if command -v git >/dev/null 2>&1; then
    _doc_row ok git "$(git --version 2>&1 | head -n1)"
  else
    _doc_row fail git "not on PATH, worktrees are impossible"
  fi

  # Which workers exist, where their binaries are and which credential file
  # each uses are all registry facts. Auth is checked by file existence only:
  # never read, never printed.
  local reg_rows name display sandbox write probe bin auth_path
  reg_rows="$(mktemp "${TMPDIR:-/tmp}/ssa-doctor-workers.XXXXXX")"
  if _ssa workers >"$reg_rows" 2>/dev/null; then
    _doc_row ok registry "$(wc -l <"$reg_rows" | tr -d ' ') worker(s) registered"
  else
    _doc_row fail registry "workers.json did not validate, no dispatch can build"
  fi
  while IFS=$'\t' read -r name display sandbox write probe bin auth_path; do
    [[ -n "$name" ]] || continue
    if [[ -n "$bin" && "$bin" != "-" && -x "$bin" ]]; then
      _doc_row ok "bin:$name" "$bin"
    else
      _doc_row warn "bin:$name" "not executable at ${bin:-<unset>}"
    fi
    if [[ -n "$auth_path" && "$auth_path" != "-" ]]; then
      _doc_auth "$name" "$auth_path"
    else
      _doc_row warn "auth:$name" "registry declares no credential file"
    fi
  done <"$reg_rows"
  rm -f "$reg_rows"
  # Which workers run with a scrubbed environment is a registry fact, so it is
  # printed from the registry rather than restated in a doc that would drift.
  local scrub_row scrub_json
  scrub_json="$(mktemp "${TMPDIR:-/tmp}/ssa-doctor-scrub.XXXXXX")"
  scrub_row=""
  if _ssa workers --json >"$scrub_json" 2>/dev/null; then
    scrub_row="$(python3 - "$scrub_json" <<'PY' 2>/dev/null || true
import json, sys
rows = json.load(open(sys.argv[1]))
on = [r["name"] for r in rows if r.get("env_scrub")]
off = [r["name"] for r in rows if not r.get("env_scrub")]
print("scrubbed: %s; inherits this environment: %s"
      % (", ".join(on) or "none", ", ".join(off) or "none"))
PY
)"
  fi
  rm -f "$scrub_json"
  if [[ -n "$scrub_row" ]]; then
    _doc_row ok env-scrub "$scrub_row"
  else
    _doc_row warn env-scrub "registry did not report env_scrub"
  fi
  if command -v security >/dev/null 2>&1; then
    if security find-generic-password -s 'Claude Code-credentials' \
        >/dev/null 2>&1; then
      _doc_row ok auth:claude "keychain item present"
    else
      _doc_row warn auth:claude "no keychain item for Claude Code-credentials"
    fi
  else
    _doc_row warn auth:claude "no security(1) on this platform, cannot check"
  fi

  local cache_dir cache_file perms age
  cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/smart-subagents"
  cache_file="$cache_dir/ai-cli-usage.json"
  if [[ -d "$cache_dir" ]]; then
    perms="$(stat -c '%a' "$cache_dir" 2>/dev/null \
      || stat -f '%OLp' "$cache_dir" 2>/dev/null || echo '?')"
    if [[ "$perms" == "700" ]]; then
      _doc_row ok cache:perms "$cache_dir is 0700"
    else
      _doc_row warn cache:perms "$cache_dir is $perms, want 0700"
    fi
    if [[ -f "$cache_file" ]]; then
      age=$(( $(_now) - $(_mtime "$cache_file") ))
      _doc_row ok cache:usage "quota cache $(_age_human "$age") old"
    else
      _doc_row warn cache:usage "no quota cache yet, first pick will be slow"
    fi
  else
    _doc_row warn cache:perms "$cache_dir does not exist yet"
  fi

  if mkdir -p "$SSA_WORK_DIR" 2>/dev/null && [[ -w "$SSA_WORK_DIR" ]]; then
    _doc_row ok workdir "$SSA_WORK_DIR writable"
  else
    _doc_row fail workdir "$SSA_WORK_DIR not writable, dispatch cannot mint a task"
  fi

  local d repos_file orphans=0 pending=0 noresume=0
  repos_file="$(mktemp "${TMPDIR:-/tmp}/ssa-doctor-repos.XXXXXX")"
  if [[ -d "$SSA_WORK_DIR" ]]; then
    for d in "$SSA_WORK_DIR"/*; do
      [[ -d "$d" ]] || continue
      # $SSA_WORK_DIR/wt holds the worktrees themselves, not task dirs.
      [[ "$(basename "$d")" != "wt" ]] || continue
      if [[ "$(basename "$d")" != plan-* && ! -f "$d/task-id.txt" ]]; then
        orphans=$(( orphans + 1 ))
        continue
      fi
      [[ ! -f "$d/repo.txt" ]] || cat "$d/repo.txt" >>"$repos_file"
      if [[ -f "$d/exit-code.txt" && ! -f "$d/outcome.json" ]]; then
        pending=$(( pending + 1 ))
      fi
      [[ ! -f "$d/resume-unavailable.txt" ]] || noresume=$(( noresume + 1 ))
    done
  fi
  if (( orphans == 0 )); then
    _doc_row ok orphans "no task dirs without task-id.txt"
  else
    _doc_row warn orphans "$orphans dir(s) without task-id.txt under $SSA_WORK_DIR"
  fi
  if (( pending == 0 )); then
    _doc_row ok verify:pending "every finished task has an outcome.json"
  else
    _doc_row warn verify:pending \
      "$pending finished task(s) unverified, run: smart-subagents.sh verify --dir DIR"
  fi
  if (( noresume == 0 )); then
    _doc_row ok resume "no tasks marked resume-unavailable"
  else
    _doc_row warn resume "$noresume task(s) have no resumable session id"
  fi

  local repo stale=0 wtpath
  if [[ -s "$repos_file" ]]; then
    while IFS= read -r repo; do
      [[ -n "$repo" && -d "$repo" ]] || continue
      while IFS= read -r wtpath; do
        [[ -n "$wtpath" ]] || continue
        [[ -d "$wtpath" ]] || stale=$(( stale + 1 ))
      done < <(git -C "$repo" worktree list --porcelain 2>/dev/null \
        | sed -n 's/^worktree //p')
    done < <(sort -u "$repos_file")
  fi
  rm -f "$repos_file"
  if (( stale == 0 )); then
    _doc_row ok worktrees "no registered worktree points at a missing path"
  else
    _doc_row warn worktrees "$stale stale worktree entr(ies), run git worktree prune"
  fi

  if [[ -n "$json" ]]; then
    python3 - "$_DOC_FILE" <<'PY'
import json, sys
rows = []
for line in open(sys.argv[1], errors="replace"):
    parts = line.rstrip("\n").split("\t", 2)
    if len(parts) == 3:
        rows.append({"state": parts[0], "check": parts[1], "detail": parts[2]})
print(json.dumps(rows, indent=2))
PY
  else
    while IFS=$'\t' read -r state check detail; do
      printf '[%-4s] %-16s %s\n' "$state" "$check" "$detail"
    done <"$_DOC_FILE"
  fi
  local fatal="$_DOC_FATAL"
  rm -f "$_DOC_FILE"
  (( fatal == 0 )) || return 1
}

_doc_auth() {
  # The path is printed with $HOME collapsed to ~: an absolute credential path
  # carries the account's username, and doctor output gets pasted around.
  local cli="$1" path="$2" shown="$2"
  [[ -z "$HOME" ]] || shown="${path/#$HOME/\~}"
  if [[ -f "$path" ]]; then
    _doc_row ok "auth:$cli" "credentials file present"
  else
    _doc_row warn "auth:$cli" "no credentials file at $shown"
  fi
}

# --- verify: machine-readable, baseline-aware ---------------------------------

cmd_verify() {
  local dir=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      *) die "verify: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "verify: --dir required"
  need git; need python3
  local wt base
  wt="$(_read1 "$dir/wt.txt")"
  base="$(_read1 "$dir/base-sha.txt")"
  [[ -n "$wt" && -d "$wt" ]] || die "verify: missing worktree ($dir/wt.txt)"
  [[ -n "$base" ]] || die "verify: missing base SHA ($dir/base-sha.txt)"

  local results="$dir/verify-results.txt"
  local final="$dir/verify-final.log"
  : >"$results"
  : >"$final"
  if [[ -f "$dir/verify-cmds.txt" ]]; then
    local cmd rc out
    while IFS= read -r cmd; do
      [[ -n "$cmd" ]] || continue
      case "$cmd" in \#*) continue ;; esac
      out="$(mktemp "${TMPDIR:-/tmp}/ssa-verify.XXXXXX")"
      rc=0
      ( cd "$wt" && eval "$cmd" ) >"$out" 2>&1 || rc=$?
      {
        echo "### $cmd (exit $rc)"
        tail -n5 "$out"
      } >>"$final"
      printf '%s\t%s\n' "$rc" "$cmd" >>"$results"
      rm -f "$out"
    done <"$dir/verify-cmds.txt"
  fi

  # Scope: every changed path must match a glob the parent declared.
  git -C "$wt" diff --name-only -z "$base" >"$dir/verify-changed.z" 2>/dev/null || :

  local secrets_ok=1
  if cmd_scan_secrets --dir "$dir"; then secrets_ok=1; else secrets_ok=0; fi

  local rc=0
  python3 - "$dir" "$secrets_ok" <<'PY' || rc=$?
import fnmatch, json, os, sys
from pathlib import Path

d = Path(sys.argv[1])
secrets_ok = sys.argv[2] == "1"

def lines(name):
    p = d / name
    if not p.exists():
        return None
    return [l for l in p.read_text(errors="replace").splitlines() if l.strip()]

results = []
for raw in (lines("verify-results.txt") or []):
    code, _, cmd = raw.partition("\t")
    results.append((int(code), cmd))

baseline_raw = lines("baseline-results.txt")
baseline = {}
if baseline_raw is not None:
    for raw in baseline_raw:
        code, _, cmd = raw.partition("\t")
        try:
            baseline[cmd] = int(code)
        except ValueError:
            continue

commands = []
new_failures = 0
any_failure = False
for code, cmd in results:
    base_exit = baseline.get(cmd) if baseline_raw is not None else None
    commands.append({"cmd": cmd, "exit": code, "baseline_exit": base_exit})
    if code != 0:
        any_failure = True
        if base_exit == 0 or (baseline_raw is not None and base_exit is None):
            new_failures += 1

scope_file = d / "scope.txt"
scope_ok = None
out_of_scope = []
changed_path = d / "verify-changed.z"
changed = []
if changed_path.exists():
    blob = changed_path.read_bytes().decode("utf-8", "replace")
    changed = [p for p in blob.split("\0") if p]
if scope_file.exists():
    globs = [g.strip() for g in scope_file.read_text().splitlines() if g.strip()]
    scope_ok = True
    for path in changed:
        if not any(fnmatch.fnmatch(path, g) for g in globs):
            scope_ok = False
            out_of_scope.append(path)
if out_of_scope:
    (d / "verify-out-of-scope.txt").write_text("\n".join(out_of_scope) + "\n")

log_path = d / "stdout.log"
log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
brief_denied = False
for line in log_text.splitlines():
    low = line.lower()
    if "brief.md" not in low:
        continue
    if any(
        needle in low
        for needle in (
            "permission denied",
            "eacces",
            "operation not permitted",
            "outside allowed",
            "outside of the allowed",
            "not allowed to",
        )
    ):
        brief_denied = True
        break

if not changed and brief_denied:
    verdict = "fail"
elif new_failures or scope_ok is False or not secrets_ok:
    verdict = "fail"
elif baseline_raw is None and any_failure:
    verdict = "inconclusive"
else:
    verdict = "pass"

gitleaks = "absent"
gl_path = d / "verify-secrets-gitleaks.txt"
if gl_path.exists():
    gitleaks = gl_path.read_text(errors="replace").strip() or "absent"

doc = {
    "schema_version": 1,
    "verify": {
        "commands": commands,
        "new_failures": new_failures,
        "scope_ok": scope_ok,
        # Bounded: one out-of-scope worker wrote 900 paths into this file and
        # every reader of outcome.json paid for them. The full list is on disk.
        "out_of_scope": out_of_scope[:25],
        "out_of_scope_count": len(out_of_scope),
        "out_of_scope_file": (
            str(d / "verify-out-of-scope.txt") if out_of_scope else None
        ),
        "secrets_ok": secrets_ok,
        # "absent" is not "clean": gitleaks was never run on this machine.
        "gitleaks": gitleaks,
        "changed_files": len(changed),
        "verdict": verdict,
    },
}
(d / "outcome.json").write_text(json.dumps(doc, indent=2) + "\n")
print("verify: verdict=%s new_failures=%d scope_ok=%s secrets_ok=%s changed=%d"
      % (verdict, new_failures, scope_ok, secrets_ok, len(changed)))
if out_of_scope:
    print("verify: out of scope: %d path(s): %s%s"
          % (len(out_of_scope), ", ".join(out_of_scope[:10]),
             " ..." if len(out_of_scope) > 10 else ""))
sys.exit({"pass": 0, "fail": 1, "inconclusive": 2}[verdict])
PY
  rm -f "$dir/verify-changed.z"
  local verdict_state
  case "$rc" in
    0) verdict_state="verified" ;;
    1) verdict_state="failed" ;;
    *) verdict_state="inconclusive" ;;
  esac
  _ssa_event "$dir" --phase "$verdict_state" --exit "$rc" \
    --artifact "$dir/outcome.json"
  _ssa_state "$dir" "$verdict_state"
  return "$rc"
}

# --- outcome ledger -----------------------------------------------------------
# One line per dispatch, no prompts, no diffs, no paths, no session ids. The
# point is to learn which worker actually finishes which kind of work.

cmd_record() {
  local dir="" outcome="" retries=0 handoff="" notes=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      --outcome) outcome="${2:-}"; shift 2 ;;
      --retries) retries="${2:-}"; shift 2 ;;
      --handoff-to) handoff="${2:-}"; shift 2 ;;
      --notes) notes="${2:-}"; shift 2 ;;
      *) die "record: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "record: --dir required"
  case "$outcome" in
    verified-pass|partial|rejected|blocked|env-blocked|rate-limited) ;;
    *) die "record: --outcome must be one of verified-pass partial rejected blocked env-blocked rate-limited" ;;
  esac
  [[ "$retries" =~ ^[0-9]+$ ]] || die "record: --retries takes a whole number"
  need python3
  mkdir -p "$SSA_STATE_DIR" 2>/dev/null || true
  chmod 700 "$SSA_STATE_DIR" 2>/dev/null || true
  python3 - "$dir" "$outcome" "$retries" "$handoff" "$notes" "$SSA_LEDGER" <<'PY'
import hashlib, json, os, re, sys, time
from pathlib import Path

d = Path(sys.argv[1])
outcome, retries, handoff, notes, ledger = sys.argv[2:7]

def read1(name, default=""):
    p = d / name
    if not p.exists():
        return default
    line = p.read_text(errors="replace").strip().splitlines()
    return line[0].strip() if line else default

def mtime(name):
    p = d / name
    try:
        return p.stat().st_mtime
    except OSError:
        return None

repo = read1("repo.txt")
# The path itself never leaves the machine, only a stable hash of it.
repo_hash = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:12] if repo else ""

args = []
p = d / "worker-args.txt"
if p.exists():
    args = [a for a in p.read_text(errors="replace").splitlines() if a.strip()]

exit_code = read1("exit-code.txt")
try:
    exit_code = int(exit_code)
except ValueError:
    exit_code = None

start, end = mtime("brief.md"), mtime("stdout.log")
wall = int(end - start) if (start and end and end >= start) else None

files = insertions = deletions = None
stat = d / "diff-stat.txt"
if stat.exists():
    tail = [l for l in stat.read_text(errors="replace").splitlines() if l.strip()]
    if tail:
        last = tail[-1]
        m = re.search(r"(\d+) files? changed", last)
        files = int(m.group(1)) if m else None
        m = re.search(r"(\d+) insertions?\(\+\)", last)
        insertions = int(m.group(1)) if m else 0
        m = re.search(r"(\d+) deletions?\(-\)", last)
        deletions = int(m.group(1)) if m else 0

verified = None
oc = d / "outcome.json"
if oc.exists():
    try:
        verified = (json.loads(oc.read_text()).get("verify") or {}).get("verdict") == "pass"
    except Exception:
        verified = None

def quota(name):
    p = d / name
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text())
    except Exception:
        return None
    out = {}
    for cli in doc.get("clis") or []:
        windows = cli.get("windows") or []
        pick = None
        for w in windows:
            if w.get("used_pct") is None:
                continue
            if pick is None or "primary" in (w.get("name") or ""):
                pick = w
        if pick is not None:
            out[cli.get("cli")] = round(float(pick["used_pct"]), 2)
    return out or None

record = {
    "schema_version": 1,
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "task_id": read1("task-id.txt", d.name),
    "repo_hash": repo_hash,
    "worker": read1("worker.txt"),
    "kind": read1("kind.txt"),
    "size": read1("size.txt"),
    "difficulty": read1("difficulty.txt"),
    "worker_args": args,
    "exit_code": exit_code,
    "wall_seconds": wall,
    "files_changed": files,
    "insertions": insertions,
    "deletions": deletions,
    "verification_passed": verified,
    "quota_before": quota("quota-before.json"),
    "quota_after": quota("quota-after.json"),
    "outcome": outcome,
    "retries": int(retries),
    "handoff_to": handoff or None,
    "notes": notes or None,
    "route": read1("route.txt") or None,
}

(d / "outcome-record.json").write_text(json.dumps(record, indent=2) + "\n")
path = Path(ledger)
path.parent.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(path.parent, 0o700)
except OSError:
    pass
existed = path.exists()
with open(path, "a") as fh:
    fh.write(json.dumps(record) + "\n")
if not existed:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
print("record: %s worker=%s outcome=%s -> %s"
      % (record["task_id"], record["worker"] or "-", outcome, path))
PY
  _ssa_event "$dir" --phase reported --artifact "$dir/outcome-record.json"
  _ssa_state "$dir" reported
}

cmd_ledger() {
  local days=7
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --days) days="${2:-}"; shift 2 ;;
      *) die "ledger: unknown arg $1" ;;
    esac
  done
  [[ "$days" =~ ^[0-9]+$ ]] || die "ledger: --days takes a whole number"
  need python3
  python3 - "$SSA_LEDGER" "$days" <<'PY'
import calendar, json, sys, time
from pathlib import Path

path, days = Path(sys.argv[1]), int(sys.argv[2])
if not path.exists():
    print("ledger: no outcomes recorded yet (%s)" % path)
    raise SystemExit(0)

cutoff = time.time() - days * 86400
rows = []
for line in path.read_text(errors="replace").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
    except Exception:
        continue
    try:
        ts = calendar.timegm(time.strptime(rec.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        ts = cutoff
    if ts >= cutoff:
        rows.append(rec)

if not rows:
    print("ledger: nothing in the last %dd (%s)" % (days, path))
    raise SystemExit(0)

by = {}
for rec in rows:
    cli = rec.get("worker") or "-"
    agg = by.setdefault(cli, {"n": 0, "pass": 0, "retries": 0, "quota": 0.0})
    agg["n"] += 1
    if rec.get("outcome") == "verified-pass":
        agg["pass"] += 1
    agg["retries"] += int(rec.get("retries") or 0)
    before, after = rec.get("quota_before") or {}, rec.get("quota_after") or {}
    if cli in before and cli in after:
        agg["quota"] += max(0.0, after[cli] - before[cli])

print("ledger: last %dd, %d dispatch(es)" % (days, len(rows)))
print("%-8s %10s %14s %12s %14s" % ("WORKER", "DISPATCH", "VERIFIED-PASS",
                                     "MEAN RETRY", "QUOTA USED %"))
for cli in sorted(by):
    agg = by[cli]
    print("%-8s %10d %13.0f%% %12.2f %13.1f%%" % (
        cli, agg["n"], 100.0 * agg["pass"] / agg["n"],
        agg["retries"] / agg["n"], agg["quota"]))
PY
}

cmd_help() {
  cat <<'EOF'
Usage: smart-subagents.sh <command> [options]

  init --repo PATH [--size tiny|small|medium|large] [--kind KIND]
       [--difficulty trivial|routine|hard|frontier] [--prefer CLI]
      Mint a private task dir, run usage, create an isolated worktree,
      pick a worker. Prints JSON with task_id, dir, worker, reason.

  pick --size SIZE [--kind KIND] [--difficulty LEVEL] [--prefer CLI]
       [--fresh] [--out DIR] [--explain]
      Print primary worker name on stdout; one summary line on stderr.
      --explain puts the full recommendation JSON on stderr instead.

  Size is how many files the task touches (gates required quota headroom).
  Difficulty is how much thinking it needs (gates worker reasoning effort).
  They are independent: a 15-line lock-free ring buffer is small and hard.

  dispatch --dir DIR [--worker CLI] [--background] [--resume]
      Run the worker against DIR/brief.md in the worktree. Captures logs +
      session id. The brief is staged as <worktree>/BRIEF.md for file-ref
      workers and removed when the run ends. With --background the worker is
      detached into its own process group, the pid lands in DIR/worker.pid, and
      a watchdog kills a run whose log and worktree both stop changing
      (SSA_STALL_SECS, default 600). --resume continues the recorded session id
      from DIR/session-id.txt and refuses when there is none. Overriding
      --worker rebinds DIR/worker-args.txt to that CLI's flags.

  ls [--all] [--state STATE]        (alias: list)
      One line per task and planning panel under SSA_WORK_DIR: age, repo,
      worker, size/difficulty/kind, inferred phase, recorded state, diff. The
      20 most recent plus everything still in flight; --all prints the rest.

  status --dir DIR
      Full detail for one task: base sha, branch, exit code, session or
      resume=unavailable, worker pid state, recorded lifecycle state and event
      count, verify state, last log lines.

  tail --dir DIR
      Follow DIR/stdout.log.

  stop --dir DIR
      TERM then KILL the recorded process group. Refuses when the pid is gone
      or belongs to a different process now.

  verify --dir DIR
      Run DIR/verify-cmds.txt in the worktree, compare against
      DIR/baseline-results.txt, check changed paths against DIR/scope.txt, run
      the secret scan, and write DIR/outcome.json. Exit 0 pass, 1 fail,
      2 inconclusive.

  cooldown --cli CLI [--clear] [--reason rate-limit|auth] [--minutes N]
      Bench a worker for every task until the cooldown expires. dispatch sets
      one by itself when a failed run's log shows a rate limit or an auth
      failure (defaults: 15 min rate-limit, 24h auth, never open-ended).

  record --dir DIR --outcome verified-pass|partial|rejected|blocked|env-blocked|rate-limited
         [--retries N] [--handoff-to CLI] [--notes STR]
      Append one outcome line to the ledger and write DIR/outcome-record.json.
      Carries no prompts, diffs, paths, session ids or account identifiers.

  ledger [--days N]
      Per-CLI dispatch count, verified-pass rate, mean retries and quota
      consumed over the last N days (default 7).

  cleanup --dir DIR [--force]
      Remove the worktree, the ssa/<id> branch and the task dir. Refuses while
      a worker is alive, the worktree is dirty, or the branch has commits the
      base does not have.

  gc [--older-than DAYS] [--dry-run|--no-dry-run] [--verbose]
      Classify every task dir as safe or kept and, with --no-dry-run, delete
      the safe ones. Dry run and 7 days by default. Kept dirs are summarized by
      reason; --verbose prints one line each. Planning panels are collected
      regardless of age once they have finished (panel-done.txt).

  doctor [--json]
      Offline health check: interpreter and git, worker binaries, credential
      files (existence only), cache perms and freshness, work dir writability,
      orphan task dirs, stale worktrees, unverified and unresumable tasks.
      Nonzero only when a new dispatch could not run at all.

  plan --repo PATH (--goal TEXT | --goal-file FILE) [--n 3] [--kind KIND]
       [--difficulty LEVEL] [--size SIZE]
      Fan the goal out to N planners with different lenses (pragmatic, risk,
      architecture, constraints), spread across the CLIs that have quota, in
      one shared read-only worktree. Prints JSON listing every plan file for a
      supervisor to consolidate. Planners are expected to leave the tree
      untouched, and the tree is checked after the run: a dirty one writes
      panel-dirty.txt and sets "dirty": true in the JSON.

  verify-summary --dir DIR          (alias: summary)
      Compact git state for the supervisor: branch, status (clipped), commits
      since base, diff stat, name-status, then the secret scan. Each section is
      clipped to 200 lines with the full text in DIR/verify-summary-full.txt.
      Exits 1 when the secret scan finds something.

  diff --dir DIR [--path PATH] [--max-bytes N]
      Bounded diff for the supervisor: the whole change as --stat, and with
      --path the unified diff of that path clipped to N bytes (default 20000).

  scan-secrets --dir DIR
      Scan added lines and newly added environment files for secrets.

Task record:
  Each task dir carries task.json (authoritative: state, class, attempts) and
  events.jsonl (append-only, one line per lifecycle point). The lifecycle is
  minted -> preflighted -> picked -> running -> exited -> verified|failed|
  inconclusive -> reported, with aborted and stalled terminal from running.

Env:
  CODEX_BIN, GROK_BIN, KIMI_BIN   override worker binary paths (the variable
                                  name comes from scripts/workers.json)
  SSA_WORKERS_JSON                alternative worker registry
  SSA_WORK_DIR                    task scratch root (default: $TMPDIR/smart-subagents)
  SSA_USAGE_PY                    override path to ai-cli-usage.py
  SSA_CLI_PY                      override path to ssa/cli.py
  SSA_PREMIUM_MODELS              model names that gate local labor (default: Fable,Opus)
  SSA_ALLOW_UNSANDBOXED_WRITE=1   accept a write dispatch to a worker with no sandbox
  SSA_ALLOW_KIMI_WRITE=1          legacy alias for SSA_ALLOW_UNSANDBOXED_WRITE
  SSA_STALL_SECS                  watchdog stall threshold (default 600)
  SSA_WATCHDOG_INTERVAL_SECS      watchdog sampling interval (default 30)
  SSA_GIT_STATUS_TIMEOUT          seconds before `git status -uall` is refused (default 5)
  SSA_GIT_STATUS_UALL_MAX         untracked lines before it is refused (default 1000)
  SSA_DEADLINE_SECS               absolute run deadline (default 0, off)
  SSA_KILL_GRACE_SECS             seconds between TERM and KILL (default 10)
  SSA_NO_QUOTA_SNAPSHOT=1         skip the post-dispatch quota snapshot
  SSA_LEDGER                      outcome ledger path
                                  (default: $XDG_STATE_HOME/smart-subagents/outcomes.jsonl)
  SSA_SHORT_HORIZON_HOURS         reset horizon that makes short-window quota free (default 4)
  SSA_FIT_HALFLIFE_DAYS           decay half-life for learned fit (default 30)
  SSA_FIT_MIN_SAMPLES             effective samples before a posterior is used (default 10)
EOF
}

main() {
  local cmd="${1:-help}"
  shift || true
  case "$cmd" in
    init) cmd_init "$@" ;;
    pick) cmd_pick "$@" ;;
    dispatch) cmd_dispatch "$@" ;;
    bg-run) cmd_bg_run "$@" ;;
    ls|list) cmd_ls "$@" ;;
    status) cmd_status "$@" ;;
    tail) cmd_tail "$@" ;;
    stop) cmd_stop "$@" ;;
    verify) cmd_verify "$@" ;;
    cooldown) cmd_cooldown "$@" ;;
    record) cmd_record "$@" ;;
    ledger) cmd_ledger "$@" ;;
    cleanup) cmd_cleanup "$@" ;;
    gc) cmd_gc "$@" ;;
    doctor) cmd_doctor "$@" ;;
    plan) cmd_plan "$@" ;;
    scan-secrets) cmd_scan_secrets "$@" ;;
    verify-summary|summary) cmd_verify_summary "$@" ;;
    diff) cmd_diff "$@" ;;
    help|-h|--help) cmd_help ;;
    *) die "unknown command: $cmd (try help)" ;;
  esac
}

main "$@"
