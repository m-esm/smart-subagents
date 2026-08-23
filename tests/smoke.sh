#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSA="$ROOT/scripts/smart-subagents.sh"
USAGE="$ROOT/scripts/ai-cli-usage.py"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/ssa-smoke.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT
export PYTHONPYCACHEPREFIX="$TEST_TMP/pycache"
# Smoke never touches the network: the post-dispatch quota snapshot is off.
export SSA_NO_QUOTA_SNAPSHOT=1

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1" >&2; exit 1; }

make_repo() {
  local repo="$1"
  mkdir -p "$repo"
  git -C "$repo" init -q
  git -C "$repo" config user.email smoke@example.invalid
  git -C "$repo" config user.name "Smoke Test"
  echo "fixture" >"$repo/README.md"
  git -C "$repo" add README.md
  git -C "$repo" commit -qm initial
}

make_task() {
  local dir="$1" wt="$2"
  mkdir -p "$dir"
  echo "$wt" >"$dir/wt.txt"
  git -C "$wt" rev-parse HEAD >"$dir/base-sha.txt"
  echo medium >"$dir/size.txt"
  : >"$dir/worker-args.txt"
  echo "Complete the fixture task." >"$dir/brief.md"
}

bash -n "$SSA" || fail "shell syntax"
python3 -m py_compile "$USAGE" || fail "python syntax"
pass "syntax checks"

secret_repo="$TEST_TMP/secret-repo"
secret_task="$TEST_TMP/secret-task"
fake_aws_key='AKIA'"1234567890ABCDEF"
make_repo "$secret_repo"
make_task "$secret_task" "$secret_repo"
printf 'AWS_KEY="%s"\n' "$fake_aws_key" >"$secret_repo/config.txt"
echo 'LOCAL_ONLY=value' >"$secret_repo/.env"
git -C "$secret_repo" add config.txt .env
if "$SSA" scan-secrets --dir "$secret_task" >/dev/null 2>&1; then
  fail "secret scan rejects findings"
fi
grep -q 'aws-access-key:' "$secret_task/verify-secrets.txt" || \
  fail "secret scan records AWS finding"
grep -q 'env-file: .env' "$secret_task/verify-secrets.txt" || \
  fail "secret scan records env file"
if grep -Fq "$fake_aws_key" "$secret_task/verify-secrets.txt"; then
  fail "secret scan masks values"
fi
pass "secret scan rejects and masks findings"
if "$SSA" verify-summary --dir "$secret_task" \
    >"$TEST_TMP/verify-summary.txt" 2>&1; then
  fail "verify summary rejects secrets"
fi
grep -q '^### secrets$' "$TEST_TMP/verify-summary.txt" || \
  fail "verify summary secrets section"
grep -q 'verify-secrets.txt' "$TEST_TMP/verify-summary.txt" || \
  fail "verify summary findings path"
pass "verify summary enforces secret gate"

clean_repo="$TEST_TMP/clean-repo"
clean_task="$TEST_TMP/clean-task"
make_repo "$clean_repo"
make_task "$clean_task" "$clean_repo"
echo 'ordinary configuration text' >"$clean_repo/config.txt"
git -C "$clean_repo" add config.txt
"$SSA" scan-secrets --dir "$clean_task" || fail "clean secret scan"
[[ ! -s "$clean_task/verify-secrets.txt" ]] || fail "clean findings file"
pass "secret scan accepts clean diff"

usage_stub="$TEST_TMP/usage-stub.sh"
cat >"$usage_stub" <<'SH'
#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["SSA_STUB_ARGV"], "w") as out:
    out.write("\n".join(sys.argv[1:]) + "\n")
print(json.dumps({
    "clis": [],
    "recommendation": {
        "primary_worker": "codex",
        "fallback_workers": [],
        "local_labor_ok": True,
        "task_size": "medium",
        "task_kind": "review",
        "difficulty": "hard",
        "target_effort": "high",
        "cross_review_required": False,
        "worker_args": {
            "codex": ["-c", "model_reasoning_effort=high"],
            "grok": ["--reasoning-effort", "high"],
            "kimi": [],
        },
        "ranked": [{"cli": "codex", "score": 80}],
        "reasons": [],
    },
}))
SH
chmod +x "$usage_stub"

init_repo="$TEST_TMP/init-repo"
init_work="$TEST_TMP/init-work"
init_argv="$TEST_TMP/init-usage-argv.txt"
make_repo "$init_repo"
SSA_WORK_DIR="$init_work" SSA_USAGE_PY="$usage_stub" \
  SSA_STUB_ARGV="$init_argv" "$SSA" init --repo "$init_repo" \
  --kind review --difficulty hard >/dev/null
init_dir="$(find "$init_work" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ "$(cat "$init_dir/kind.txt")" == "review" ]] || fail "init kind file"
grep -qx -- '--task-kind' "$init_argv" || fail "init task kind flag"
grep -qx -- 'review' "$init_argv" || fail "init task kind value"
pass "init plumbs kind"

dispatch_repo="$TEST_TMP/dispatch-repo"
dispatch_task="$TEST_TMP/dispatch-task"
codex_fail="$TEST_TMP/codex-fail.sh"
make_repo "$dispatch_repo"
make_task "$dispatch_task" "$dispatch_repo"
cat >"$codex_fail" <<'SH'
#!/bin/sh
exit 7
SH
chmod +x "$codex_fail"
if CODEX_BIN="$codex_fail" "$SSA" dispatch --dir "$dispatch_task" \
    --worker codex >/dev/null 2>&1; then
  fail "dispatch returns worker failure"
else
  dispatch_rc=$?
fi
[[ "$dispatch_rc" == "7" ]] || fail "dispatch exit status"
[[ "$(cat "$dispatch_task/exit-code.txt")" == "7" ]] || \
  fail "dispatch exit-code file"
[[ -f "$dispatch_task/resume-unavailable.txt" ]] || \
  fail "dispatch resume marker"
pass "dispatch captures exit code and unavailable resume"

kimi_task="$TEST_TMP/kimi-task"
kimi_stub="$TEST_TMP/kimi-stub.sh"
kimi_error="$TEST_TMP/kimi-error.txt"
make_task "$kimi_task" "$dispatch_repo"
if KIMI_BIN="$kimi_stub" "$SSA" dispatch --dir "$kimi_task" \
    --worker kimi >"$TEST_TMP/kimi-refusal.out" 2>"$kimi_error"; then
  fail "kimi default refusal"
fi
grep -q 'kimi has no sandbox' "$kimi_error" || fail "kimi refusal message"
grep -q 'SSA_ALLOW_KIMI_WRITE=1' "$kimi_error" || fail "kimi override message"
cat >"$kimi_stub" <<'SH'
#!/bin/sh
echo '{"type":"session.resume_hint","session_id":"kimi-session-123"}'
SH
chmod +x "$kimi_stub"
SSA_ALLOW_KIMI_WRITE=1 KIMI_BIN="$kimi_stub" \
  "$SSA" dispatch --dir "$kimi_task" --worker kimi >/dev/null || \
  fail "kimi override dispatch"
[[ -f "$kimi_task/kimi-override.txt" ]] || fail "kimi override record"
pass "kimi write refusal and override"

python3 - "$USAGE" <<'PY' || fail "recommend quota floor"
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("ai_cli_usage", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
statuses = [
    module.CliStatus(cli=name, available=True, eligible=True, score=10)
    for name in module.WORKER_CLIS
]
hard = module.recommend(statuses, task_size="medium", difficulty="hard")
routine = module.recommend(statuses, task_size="medium", difficulty="routine")
assert hard["primary_worker"] is None
assert routine["primary_worker"] is not None
assert "not dispatching on fumes" in " ".join(hard["reasons"])
PY
pass "recommend enforces hard quota floor"

plan_repo="$TEST_TMP/plan-repo"
plan_work="$TEST_TMP/plan-work"
plan_usage_argv="$TEST_TMP/plan-usage-argv.txt"
plan_codex_argv="$TEST_TMP/plan-codex-argv.txt"
codex_plan="$TEST_TMP/codex-plan.sh"
make_repo "$plan_repo"
cat >"$codex_plan" <<'SH'
#!/bin/sh
printf '%s\n' "$@" >"$SSA_CODEX_ARGV"
out=""
want_out=0
for arg in "$@"; do
  if [ "$want_out" = 1 ]; then
    out="$arg"
    want_out=0
  elif [ "$arg" = "-o" ]; then
    want_out=1
  fi
done
[ -n "$out" ] && echo '# Fixture plan' >"$out"
exit 0
SH
chmod +x "$codex_plan"
SSA_WORK_DIR="$plan_work" SSA_USAGE_PY="$usage_stub" \
  SSA_STUB_ARGV="$plan_usage_argv" SSA_CODEX_ARGV="$plan_codex_argv" \
  CODEX_BIN="$codex_plan" "$SSA" plan --repo "$plan_repo" --n 1 \
  --kind review --difficulty hard --goal "Plan the fixture." >/dev/null
grep -qx -- '-c' "$plan_codex_argv" || fail "plan effort option"
grep -qx -- 'model_reasoning_effort=high' "$plan_codex_argv" || \
  fail "plan effort value"
plan_dir="$(find "$plan_work" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ "$(cat "$plan_dir/kind.txt")" == "review" ]] || fail "plan kind file"
grep -qx -- '--task-kind' "$plan_usage_argv" || fail "plan task kind flag"
pass "plan applies worker args and kind"

ops_repo="$TEST_TMP/ops-repo"
ops_work="$TEST_TMP/ops-work"
ops_state="$TEST_TMP/ops-state"
ops_argv="$TEST_TMP/ops-usage-argv.txt"
make_repo "$ops_repo"
ssa_ops() {
  SSA_WORK_DIR="$ops_work" SSA_USAGE_PY="$usage_stub" SSA_STUB_ARGV="$ops_argv" \
    XDG_STATE_HOME="$ops_state" "$SSA" "$@"
}
ssa_ops init --repo "$ops_repo" --size small --difficulty routine --kind impl \
  >/dev/null || fail "ops init"
ops_dir="$(find "$ops_work" -mindepth 1 -maxdepth 1 -type d | head -1)"
ops_wt="$(cat "$ops_dir/wt.txt")"
ops_id="$(cat "$ops_dir/task-id.txt")"
echo "Complete the fixture task." >"$ops_dir/brief.md"

ssa_ops ls >"$TEST_TMP/ls.txt" || fail "ls runs"
grep -q "$ops_id" "$TEST_TMP/ls.txt" || fail "ls lists the task"
grep -q 'ops-repo' "$TEST_TMP/ls.txt" || fail "ls names the repo"
grep -q 'small/routine/impl' "$TEST_TMP/ls.txt" || fail "ls shows the class"
grep -q 'briefed' "$TEST_TMP/ls.txt" || fail "ls infers the phase"
ssa_ops status --dir "$ops_dir" >"$TEST_TMP/status.txt" || fail "status runs"
grep -q "base sha  : $(cat "$ops_dir/base-sha.txt")" "$TEST_TMP/status.txt" || \
  fail "status shows base sha"
grep -q "branch    : ssa/$ops_id" "$TEST_TMP/status.txt" || fail "status shows branch"
grep -q 'session   : resume=unavailable' "$TEST_TMP/status.txt" || \
  fail "status shows resume state"
grep -q 'worker pid: - (none)' "$TEST_TMP/status.txt" || fail "status shows pid state"
pass "ls and status render a task dir"

printf 'true\n' >"$ops_dir/verify-cmds.txt"
printf '0\ttrue\n' >"$ops_dir/baseline-results.txt"
ssa_ops verify --dir "$ops_dir" >"$TEST_TMP/verify-pass.txt" || fail "verify pass exit"
grep -q '"verdict": "pass"' "$ops_dir/outcome.json" || fail "verify pass verdict"
grep -q '### true (exit 0)' "$ops_dir/verify-final.log" || fail "verify final log"
printf 'true\nfalse\n' >"$ops_dir/verify-cmds.txt"
printf '0\ttrue\n0\tfalse\n' >"$ops_dir/baseline-results.txt"
verify_rc=0
ssa_ops verify --dir "$ops_dir" >"$TEST_TMP/verify-fail.txt" || verify_rc=$?
[[ "$verify_rc" == "1" ]] || fail "verify fail exit code"
grep -q '"verdict": "fail"' "$ops_dir/outcome.json" || fail "verify fail verdict"
grep -q '"new_failures": 1' "$ops_dir/outcome.json" || fail "verify counts new failures"
rm "$ops_dir/baseline-results.txt"
verify_rc=0
ssa_ops verify --dir "$ops_dir" >"$TEST_TMP/verify-incon.txt" || verify_rc=$?
[[ "$verify_rc" == "2" ]] || fail "verify inconclusive exit code"
grep -q '"verdict": "inconclusive"' "$ops_dir/outcome.json" || \
  fail "verify inconclusive verdict"
printf 'true\n' >"$ops_dir/verify-cmds.txt"
printf '0\ttrue\n' >"$ops_dir/baseline-results.txt"
ssa_ops verify --dir "$ops_dir" >/dev/null || fail "verify pass again"
pass "verify reports pass, fail and inconclusive"

echo 0 >"$ops_dir/exit-code.txt"
ssa_ops record --dir "$ops_dir" --outcome verified-pass --retries 2 \
  >"$TEST_TMP/record.txt" || fail "record runs"
ledger_file="$ops_state/smart-subagents/outcomes.jsonl"
[[ -f "$ledger_file" ]] || fail "record writes the ledger"
[[ -f "$ops_dir/outcome-record.json" ]] || fail "record writes the task copy"
grep -q '"outcome": "verified-pass"' "$ledger_file" || fail "record stores the outcome"
if grep -Fq "$ops_repo" "$ledger_file"; then
  fail "record masks the repo path"
fi
if grep -Fq "$ops_dir" "$ledger_file"; then
  fail "record omits the task dir path"
fi
ssa_ops ledger --days 7 >"$TEST_TMP/ledger.txt" || fail "ledger runs"
grep -q '1 dispatch' "$TEST_TMP/ledger.txt" || fail "ledger counts the dispatch"
grep -q '^codex' "$TEST_TMP/ledger.txt" || fail "ledger groups by worker"
pass "record and ledger keep an outcome trail"

ssa_ops gc --older-than 0 >"$TEST_TMP/gc.txt" || fail "gc dry run"
grep -q "safe  task $ops_id" "$TEST_TMP/gc.txt" || fail "gc lists safe dirs"
grep -q 'dry run' "$TEST_TMP/gc.txt" || fail "gc says it is a dry run"
[[ -d "$ops_dir" ]] || fail "gc dry run deletes nothing"
pass "gc dry run lists without deleting"

echo scratch >"$ops_wt/untracked.txt"
cleanup_rc=0
ssa_ops cleanup --dir "$ops_dir" >"$TEST_TMP/cleanup-refuse.txt" || cleanup_rc=$?
[[ "$cleanup_rc" == "1" ]] || fail "cleanup refuses dirty worktree"
grep -q 'uncommitted changes' "$TEST_TMP/cleanup-refuse.txt" || \
  fail "cleanup explains the refusal"
[[ -d "$ops_dir" ]] || fail "cleanup kept the dirty task"
rm "$ops_wt/untracked.txt"
ssa_ops cleanup --dir "$ops_dir" >"$TEST_TMP/cleanup-ok.txt" || fail "cleanup clean"
[[ ! -d "$ops_dir" ]] || fail "cleanup removes the task dir"
git -C "$ops_repo" branch --list "ssa/$ops_id" >"$TEST_TMP/branches.txt"
[[ ! -s "$TEST_TMP/branches.txt" ]] || fail "cleanup deletes the ssa branch"
pass "cleanup refuses dirty then removes clean"

bg_dir=""
ssa_ops init --repo "$ops_repo" --size small --difficulty routine \
  >/dev/null || fail "background init"
bg_dir="$(find "$ops_work" -mindepth 1 -maxdepth 1 -type d | head -1)"
echo "Complete the fixture task." >"$bg_dir/brief.md"
bg_codex="$TEST_TMP/codex-slow.sh"
cat >"$bg_codex" <<'SH'
#!/bin/sh
echo running
sleep 60
SH
chmod +x "$bg_codex"
CODEX_BIN="$bg_codex" ssa_ops dispatch --dir "$bg_dir" --worker codex \
  --background >"$TEST_TMP/bg.txt" || fail "background dispatch"
grep -q 'background worker=codex pid=' "$TEST_TMP/bg.txt" || fail "background prints pid"
bg_waited=0
while (( bg_waited < 50 )); do
  [[ "$(SSA_WORK_DIR="$ops_work" "$SSA" status --dir "$bg_dir" \
    | sed -n 's/^phase     : //p')" != "running" ]] || break
  sleep 0.2
  bg_waited=$(( bg_waited + 1 ))
done
(( bg_waited < 50 )) || fail "background worker reaches running phase"
ssa_ops stop --dir "$bg_dir" >"$TEST_TMP/stop.txt" || fail "stop signals the group"
grep -q 'signalled process group' "$TEST_TMP/stop.txt" || fail "stop reports the group"
[[ -f "$bg_dir/stopped.txt" ]] || fail "stop records the stop"
stop_rc=0
ssa_ops stop --dir "$bg_dir" >"$TEST_TMP/stop-again.txt" 2>&1 || stop_rc=$?
[[ "$stop_rc" == "1" ]] || fail "stop refuses a dead worker"
grep -q 'is not running' "$TEST_TMP/stop-again.txt" || fail "stop reports dead state"
pass "background dispatch, status and stop"

doctor_home="$TEST_TMP/doctor-home"
doctor_work="$TEST_TMP/doctor-work"
mkdir -p "$doctor_home" "$doctor_work"
HOME="$doctor_home" XDG_CACHE_HOME="$doctor_home/.cache" \
  SSA_WORK_DIR="$doctor_work" CODEX_BIN="$TEST_TMP/no-codex" \
  GROK_BIN="$TEST_TMP/no-grok" KIMI_BIN="$TEST_TMP/no-kimi" \
  "$SSA" doctor >"$TEST_TMP/doctor.txt" || fail "doctor exits zero when warnings only"
grep -q '^\[ok  \] python3' "$TEST_TMP/doctor.txt" || fail "doctor checks python3"
grep -q '^\[warn\] bin:codex' "$TEST_TMP/doctor.txt" || fail "doctor warns on missing bin"
if grep -q '^\[fail\]' "$TEST_TMP/doctor.txt"; then
  fail "doctor has no fatal findings on a healthy fixture"
fi
HOME="$doctor_home" XDG_CACHE_HOME="$doctor_home/.cache" \
  SSA_WORK_DIR="$doctor_work" "$SSA" doctor --json >"$TEST_TMP/doctor.json" \
  || fail "doctor --json exits zero"
python3 - "$TEST_TMP/doctor.json" <<'PY' || fail "doctor --json is a JSON array"
import json, sys
rows = json.load(open(sys.argv[1]))
assert isinstance(rows, list) and rows
assert {"state", "check", "detail"} <= set(rows[0])
PY
pass "doctor reports health without failing on warnings"

echo "PASS all smoke checks"
