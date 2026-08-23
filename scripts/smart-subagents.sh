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

  local task_id dir
  task_id="$(date +%s)-$$"
  mkdir -p "$SSA_WORK_DIR"
  chmod 700 "$SSA_WORK_DIR" 2>/dev/null || true
  dir="${SSA_WORK_DIR}/${task_id}"
  mkdir -m 700 -p "$dir"

  echo "$task_id" >"$dir/task-id.txt"
  echo "$repo" >"$dir/repo.txt"
  echo "$size" >"$dir/size.txt"
  echo "$difficulty" >"$dir/difficulty.txt"
  echo "$kind" >"$dir/kind.txt"
  git -C "$repo" rev-parse HEAD >"$dir/base-sha.txt"
  git -C "$repo" rev-parse --abbrev-ref HEAD >"$dir/repo-branch.txt"
  git -C "$repo" status --porcelain -uall >"$dir/repo-status.txt" || true

  # Parallel: usage + worktree
  python3 "$USAGE_PY" --json --task-size "$size" --difficulty "$difficulty" \
    --task-kind "$kind" \
    >"$dir/usage.json" &
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
(d / "pick.json").write_text(json.dumps(pick_doc, indent=2))
(d / "worker.txt").write_text(pick + ("\n" if pick else ""))
print(json.dumps({"task_id": d.name, "dir": str(d), **pick_doc}, indent=2))
PY
}

cmd_pick() {
  local size="medium" out="" preferred="" difficulty="routine" kind="default" fresh=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --size) size="${2:-}"; shift 2 ;;
      --difficulty) difficulty="${2:-}"; shift 2 ;;
      --kind) kind="${2:-}"; shift 2 ;;
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
  python3 "$USAGE_PY" --json --task-size "$size" --difficulty "$difficulty" \
    --task-kind "$kind" \
    ${fresh[@]+"${fresh[@]}"} >"$tmp"
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

  # Difficulty-derived flags come from the recommender, never re-derived here.
  local wargs=()
  if [[ -f "$dir/worker-args.txt" ]]; then
    while IFS= read -r _a; do [[ -n "$_a" ]] && wargs+=("$_a"); done \
      <"$dir/worker-args.txt"
  fi

  echo "$worker" >"$dir/worker.txt"
  local log="$dir/stdout.log"
  local sid="" rc=0

  case "$worker" in
    codex)
      [[ -x "$CODEX_BIN" ]] || die "codex not found at $CODEX_BIN"
      local codex
      codex="$CODEX_BIN"
      # shellcheck disable=SC2094
      if "$codex" exec -C "$wt" -s workspace-write --json \
          ${wargs[@]+"${wargs[@]}"} \
          -o "$dir/last-msg.txt" - <"$brief" >"$log" 2>&1; then
        rc=0
      else
        rc=$?
      fi
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
      if "$GROK_BIN" -p "$prompt" --cwd "$wt" --sandbox workspace \
          ${wargs[@]+"${wargs[@]}"} \
          --output-format json >"$log" 2>&1; then
        rc=0
      else
        rc=$?
      fi
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
      [[ "${SSA_ALLOW_KIMI_WRITE:-}" == "1" ]] || \
        die "dispatch: kimi has no sandbox and write dispatch is blocked; set SSA_ALLOW_KIMI_WRITE=1 to override the risk"
      [[ -x "$KIMI_BIN" ]] || die "kimi not found at $KIMI_BIN"
      date -u '+%Y-%m-%dT%H:%M:%SZ' >"$dir/kimi-override.txt"
      if (
        cd "$wt"
        env -i HOME="$HOME" PATH="$PATH" TMPDIR="${TMPDIR:-/tmp}" \
          TERM="${TERM:-dumb}" "$KIMI_BIN" \
          -p "Read the file ${brief} and complete the task it describes." \
          --output-format stream-json ${wargs[@]+"${wargs[@]}"}
      ) >"$log" 2>&1; then
        rc=0
      else
        rc=$?
      fi
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

  echo "$rc" >"$dir/exit-code.txt"
  echo "$sid" >"$dir/session-id.txt"
  if [[ -z "$sid" ]]; then
    echo "worker did not emit a resumable session id" >"$dir/resume-unavailable.txt"
  fi
  # Diff stat for supervisor
  local base
  base="$(cat "$dir/base-sha.txt" 2>/dev/null || true)"
  if [[ -n "$base" ]]; then
    git -C "$wt" diff --stat "$base" >"$dir/diff-stat.txt" 2>/dev/null || true
    git -C "$wt" status --porcelain -uall >"$dir/wt-status.txt" 2>/dev/null || true
  fi
  local resume="unavailable"
  [[ -n "$sid" ]] && resume="available"
  echo "smart-subagents: worker=$worker session=${sid:-unavailable}" \
    "resume=$resume exit=$rc" \
    "args=${wargs[*]:-defaults} log=$log"
  return "$rc"
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
  mkdir -p "$SSA_WORK_DIR"; chmod 700 "$SSA_WORK_DIR" 2>/dev/null || true
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
  local wt="$dir/wt"
  git -C "$repo" worktree add --detach "$wt" >/dev/null 2>&1 \
    || die "plan: worktree add failed for $repo"
  echo "$wt" >"$dir/wt.txt"

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
      local wargs=()
      if [[ -f "$dir/worker-args-$worker.txt" ]]; then
        while IFS= read -r _a; do [[ -n "$_a" ]] && wargs+=("$_a"); done \
          <"$dir/worker-args-$worker.txt"
      fi
      case "$worker" in
        codex)
          local cx; cx="$CODEX_BIN"
          [[ -x "$cx" ]] || exit 127
          "$cx" exec -C "$wt" -s read-only ${wargs[@]+"${wargs[@]}"} \
            -o "$plan_out" - <"$brief" \
            >"$dir/plan-$i.log" 2>&1 || true
          ;;
        grok)
          "$GROK_BIN" -p "$(cat "$brief")" --cwd "$wt" --sandbox workspace \
            ${wargs[@]+"${wargs[@]}"} \
            >"$plan_out" 2>"$dir/plan-$i.log" || true
          ;;
        kimi)
          ( cd "$wt" && env -i HOME="$HOME" PATH="$PATH" \
              TMPDIR="${TMPDIR:-/tmp}" TERM="${TERM:-dumb}" "$KIMI_BIN" \
              -p "Read $brief and complete the task it describes." \
              ${wargs[@]+"${wargs[@]}"} ) \
            >"$plan_out" 2>"$dir/plan-$i.log" || true
          ;;
      esac
      [[ -s "$plan_out" ]] || echo "(planner produced no output; see plan-$i.log)" >"$plan_out"
    ) &
    pids+=($!)
  done

  local pid
  for pid in "${pids[@]}"; do wait "$pid" || true; done

  python3 - "$dir" "${lenses[@]}" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
plans = []
for f in sorted(d.glob("plan-*-*.md")):
    text = f.read_text(errors="replace").strip()
    plans.append({"file": str(f), "lens": f.stem.split("-")[2],
                  "worker": f.stem.split("-")[3], "bytes": len(text),
                  "empty": text.startswith("(planner produced no output")})
print(json.dumps({"dir": str(d), "worktree": str(d / "wt"),
                  "goal": str(d / "goal.md"), "planners": sys.argv[2:],
                  "plans": plans,
                  "next": "supervisor: read every plan, reconcile disagreements, "
                          "emit one plan"}, indent=2))
PY
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
  : >"$findings"
  git -C "$wt" diff --no-ext-diff --unified=0 "$base" -- >"$diff_file"
  git -C "$wt" diff --name-status --diff-filter=A "$base" -- >"$names_file"
  python3 - "$diff_file" "$added_file" "$names_file" "$findings" <<'PY'
import math
import re
import sys
from collections import Counter

diff_path, added_path, names_path, findings_path = sys.argv[1:]
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

for raw in open(names_path, errors="replace"):
    fields = raw.rstrip("\n").split("\t", 1)
    if len(fields) == 2 and env_re.search(fields[1]):
        findings.append("env-file: " + fields[1])

with open(added_path, "w") as added:
    for raw in open(diff_path, errors="replace"):
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:].rstrip("\n")
        added.write(line + "\n")
        matches = []
        names = []
        for name, pattern in patterns:
            found = list(pattern.finditer(line))
            if found:
                names.append(name)
                matches.extend(found)
        entropy_matches = [
            match for match in token_re.finditer(line)
            if entropy(match.group(0)) > 4.5
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

  if command -v gitleaks >/dev/null 2>&1; then
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
  rm -f "$diff_file" "$added_file" "$names_file"
  [[ ! -s "$findings" ]]
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
  echo "### secrets"
  if cmd_scan_secrets --dir "$dir"; then
    echo "PASS"
  else
    echo "FAIL: $dir/verify-secrets.txt"
    return 1
  fi
}

cmd_help() {
  cat <<'EOF'
Usage: smart-subagents.sh <command> [options]

  init --repo PATH [--size tiny|small|medium|large] [--kind KIND]
       [--difficulty trivial|routine|hard|frontier] [--prefer CLI]
      Mint a private task dir, run usage, create an isolated worktree,
      pick a worker. Prints JSON with task_id, dir, worker, reason.

  pick --size SIZE [--kind KIND] [--difficulty LEVEL] [--prefer CLI]
       [--fresh] [--out DIR]
      Print primary worker name on stdout; recommendation JSON on stderr.

  Size is how many files the task touches (gates required quota headroom).
  Difficulty is how much thinking it needs (gates worker reasoning effort).
  They are independent: a 15-line lock-free ring buffer is small and hard.

  dispatch --dir DIR [--worker CLI]
      Run the worker against DIR/brief.md in the worktree. Captures logs + session id.

  plan --repo PATH (--goal TEXT | --goal-file FILE) [--n 3] [--kind KIND]
       [--difficulty LEVEL] [--size SIZE]
      Fan the goal out to N planners with different lenses (pragmatic, risk,
      architecture, constraints), spread across the CLIs that have quota, in
      one shared read-only worktree. Prints JSON listing every plan file for a
      supervisor to consolidate. Planners never write to the tree.

  verify-summary --dir DIR
      Compact git state for the supervisor (stat/name-status only).

  scan-secrets --dir DIR
      Scan added lines and newly added environment files for secrets.

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
    plan) cmd_plan "$@" ;;
    scan-secrets) cmd_scan_secrets "$@" ;;
    verify-summary|summary) cmd_verify_summary "$@" ;;
    help|-h|--help) cmd_help ;;
    *) die "unknown command: $cmd (try help)" ;;
  esac
}

main "$@"
