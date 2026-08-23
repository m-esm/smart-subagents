#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSA="$ROOT/scripts/smart-subagents.sh"
USAGE="$ROOT/scripts/ai-cli-usage.py"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/ssa-smoke.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT
export PYTHONPYCACHEPREFIX="$TEST_TMP/pycache"

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

echo "PASS all smoke checks"
