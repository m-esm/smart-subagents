#!/usr/bin/env bash
# smart-subagents.sh — orchestration helpers for the smart-subagents supervisor.
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

USAGE_PY="${SSA_USAGE_PY:-${SSA_ROOT}/scripts/ai-cli-usage.py}"
[[ -f "$USAGE_PY" ]] || USAGE_PY="${SCRIPT_DIR}/ai-cli-usage.py"

# Work dir holds briefs, worktrees and worker logs: private, never world-readable.
SSA_WORK_DIR="${SSA_WORK_DIR:-${TMPDIR:-/tmp}/smart-subagents}"

CODEX_BIN="${CODEX_BIN:-$(command -v codex || true)}"
GROK_BIN="${GROK_BIN:-${HOME}/.grok/bin/grok}"
KIMI_BIN="${KIMI_BIN:-${HOME}/.kimi-code/bin/kimi}"

die() { echo "smart-subagents: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing $1"; }

cmd_init() {
  local repo="" size="medium" preferred=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo) repo="${2:-}"; shift 2 ;;
      --size) size="${2:-}"; shift 2 ;;
      --prefer) preferred="${2:-}"; shift 2 ;;
      *) die "init: unknown arg $1" ;;
    esac
  done
  [[ -n "$repo" ]] || die "init: --repo required"
  [[ -d "$repo" ]] || die "init: repo not a directory: $repo"
  need git
  need python3

  local task_id dir
  task_id="$(date +%s)-$$"
  mkdir -p "$SSA_WORK_DIR"
  chmod 700 "$SSA_WORK_DIR" 2>/dev/null || true
  dir="${SSA_WORK_DIR}/${task_id}"
  mkdir -m 700 -p "$dir"

  echo "$task_id" >"$dir/task-id.txt"
  echo "$repo" >"$dir/repo.txt"
  echo "$size" >"$dir/size.txt"
  git -C "$repo" rev-parse HEAD >"$dir/base-sha.txt"
  git -C "$repo" rev-parse --abbrev-ref HEAD >"$dir/repo-branch.txt"
  git -C "$repo" status --porcelain -uall >"$dir/repo-status.txt" || true

  # Parallel: usage + worktree
  python3 "$USAGE_PY" --json --task-size "$size" >"$dir/usage.json" &
  local usage_pid=$!

  local wt="$dir/wt"
  if git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # git worktree prints progress on stdout — keep JSON clean
    git -C "$repo" worktree add "$wt" -b "ssa/${task_id}" >/dev/null 2>&1 \
      || die "init: worktree add failed for $repo"
    echo "$wt" >"$dir/wt.txt"
  else
    echo "NOT_GIT" >"$dir/wt.txt"
    wt=""
  fi

  wait "$usage_pid" || true

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
    "ranked": ranked,
    "reasons": rec.get("reasons") or [],
}
(d / "pick.json").write_text(json.dumps(pick_doc, indent=2))
(d / "worker.txt").write_text(pick + ("\n" if pick else ""))
print(json.dumps({"task_id": d.name, "dir": str(d), **pick_doc}, indent=2))
PY
}

cmd_pick() {
  local size="medium" out="" preferred="" fresh=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --size) size="${2:-}"; shift 2 ;;
      --out) out="${2:-}"; shift 2 ;;
      --prefer) preferred="${2:-}"; shift 2 ;;
      --fresh) fresh=(--fresh); shift ;;
      *) die "pick: unknown arg $1" ;;
    esac
  done
  need python3
  local tmp
  tmp="$(mktemp)"
  # bash 3.2 + set -u: an empty array must be expanded defensively
  python3 "$USAGE_PY" --json --task-size "$size" ${fresh[@]+"${fresh[@]}"} >"$tmp"
  if [[ -n "$out" ]]; then
    mkdir -p "$out"
    cp "$tmp" "$out/usage.json"
  fi
  python3 - "$tmp" "$preferred" <<'PY'
import json, sys
usage = json.loads(open(sys.argv[1]).read())
preferred = (sys.argv[2] or "").strip().lower()
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
print(json.dumps(rec, indent=2), file=sys.stderr)
PY
  rm -f "$tmp"
}

cmd_dispatch() {
  local dir="" worker=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir) dir="${2:-}"; shift 2 ;;
      --worker) worker="${2:-}"; shift 2 ;;
      *) die "dispatch: unknown arg $1" ;;
    esac
  done
  [[ -n "$dir" && -d "$dir" ]] || die "dispatch: --dir required"
  worker="${worker:-$(cat "$dir/worker.txt" 2>/dev/null || true)}"
  [[ -n "$worker" ]] || die "dispatch: no worker"
  local brief="$dir/brief.md"
  [[ -f "$brief" ]] || die "dispatch: missing $brief"
  local wt
  wt="$(cat "$dir/wt.txt" 2>/dev/null || true)"
  [[ -n "$wt" && -d "$wt" ]] || die "dispatch: missing worktree ($dir/wt.txt)"
  local size
  size="$(cat "$dir/size.txt" 2>/dev/null || echo medium)"

  echo "$worker" >"$dir/worker.txt"
  local log="$dir/stdout.log"
  local sid=""

  case "$worker" in
    codex)
      [[ -x "$CODEX_BIN" || -n "$(command -v codex)" ]] || die "codex not found"
      local codex
      codex="$(command -v codex || echo "$CODEX_BIN")"
      # shellcheck disable=SC2094
      "$codex" exec -C "$wt" -s workspace-write --json \
        -o "$dir/last-msg.txt" - <"$brief" >"$log" 2>&1 || true
      # best-effort session id from jsonl
      sid="$(python3 - "$log" <<'PY' || true
import json,sys,re
path=sys.argv[1]
sid=""
try:
  for line in open(path, errors="replace"):
    line=line.strip()
    if not line.startswith("{"): continue
    try: o=json.loads(line)
    except: continue
    for k in ("session_id","sessionId","id"):
      if isinstance(o.get(k), str) and len(o[k])>8: sid=o[k]
    t=o.get("thread") or o.get("thread_id")
    if isinstance(t,str) and len(t)>8: sid=t
except: pass
print(sid)
PY
)"
      ;;
    grok)
      [[ -x "$GROK_BIN" ]] || die "grok not found at $GROK_BIN"
      # Pass brief via stdin-friendly path: write prompt file
      local prompt
      prompt="$(cat "$brief")"
      "$GROK_BIN" -p "$prompt" --cwd "$wt" --sandbox workspace \
        --output-format json >"$log" 2>&1 || true
      sid="$(python3 - "$log" <<'PY' || true
import json,sys
text=open(sys.argv[1],errors="replace").read().strip()
sid=""
# whole-file JSON
for candidate in (text, text[text.rfind("{"):] if "{" in text else ""):
  if not candidate: continue
  try:
    o=json.loads(candidate)
  except Exception:
    continue
  for k in ("sessionId","session_id"):
    if isinstance(o.get(k), str):
      sid=o[k]
print(sid)
PY
)"
      ;;
    kimi)
      [[ -x "$KIMI_BIN" ]] || die "kimi not found at $KIMI_BIN"
      local model_args=()
      if [[ "$size" == "tiny" || "$size" == "small" ]]; then
        model_args=(-m kimi-for-coding-highspeed)
      fi
      (
        cd "$wt"
        "$KIMI_BIN" -p "Read the file ${brief} and complete the task it describes." \
          --output-format stream-json ${model_args[@]+"${model_args[@]}"}
      ) >"$log" 2>&1 || true
      sid="$(python3 - "$log" <<'PY' || true
import json,sys
sid=""
for line in open(sys.argv[1], errors="replace"):
  line=line.strip()
  if not line.startswith("{"): continue
  try: o=json.loads(line)
  except: continue
  if o.get("type")=="session.resume_hint" and o.get("session_id"):
    sid=o["session_id"]
  if isinstance(o.get("session_id"), str):
    sid=o["session_id"]
print(sid)
PY
)"
      ;;
    *) die "dispatch: unknown worker $worker" ;;
  esac

  echo "$sid" >"$dir/session-id.txt"
  # Diff stat for supervisor
  local base
  base="$(cat "$dir/base-sha.txt" 2>/dev/null || true)"
  if [[ -n "$base" ]]; then
    git -C "$wt" diff --stat "$base" >"$dir/diff-stat.txt" 2>/dev/null || true
    git -C "$wt" status --porcelain -uall >"$dir/wt-status.txt" 2>/dev/null || true
  fi
  echo "smart-subagents: worker=$worker session=${sid:-unknown} log=$log"
  [[ -n "$sid" ]] || return 0
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
  local wt base
  wt="$(cat "$dir/wt.txt")"
  base="$(cat "$dir/base-sha.txt")"
  echo "### branch"; git -C "$wt" branch --show-current
  echo "### status"; git -C "$wt" status --porcelain -uall
  echo "### commits since base"; git -C "$wt" log --oneline "${base}..HEAD" || true
  echo "### diff stat"; git -C "$wt" diff --stat "$base" || true
  echo "### name-status"; git -C "$wt" diff --name-status "$base" || true
}

cmd_help() {
  cat <<'EOF'
Usage: smart-subagents.sh <command> [options]

  init --repo PATH [--size tiny|small|medium|large] [--prefer CLI]
      Mint a private task dir, run usage, create an isolated worktree,
      pick a worker. Prints JSON with task_id, dir, worker, reason.

  pick --size SIZE [--prefer CLI] [--fresh] [--out DIR]
      Print primary worker name on stdout; recommendation JSON on stderr.

  dispatch --dir DIR [--worker CLI]
      Run the worker against DIR/brief.md in the worktree. Captures logs + session id.

  verify-summary --dir DIR
      Compact git state for the supervisor (stat/name-status only).

Env:
  CODEX_BIN, GROK_BIN, KIMI_BIN   override worker binary paths
  SSA_WORK_DIR                    task scratch root (default: $TMPDIR/smart-subagents)
  SSA_USAGE_PY                    override path to ai-cli-usage.py
  SSA_PREMIUM_MODELS              model names that gate local labor (default: Fable,Opus)
EOF
}

main() {
  local cmd="${1:-help}"
  shift || true
  case "$cmd" in
    init) cmd_init "$@" ;;
    pick) cmd_pick "$@" ;;
    dispatch) cmd_dispatch "$@" ;;
    verify-summary|summary) cmd_verify_summary "$@" ;;
    help|-h|--help) cmd_help ;;
    *) die "unknown command: $cmd (try help)" ;;
  esac
}

main "$@"
