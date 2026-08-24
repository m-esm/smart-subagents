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
python3 -m py_compile "$USAGE" "$ROOT"/scripts/ssa/*.py || fail "python syntax"
pass "syntax checks"

python3 "$ROOT/scripts/ssa/cli.py" registry-validate >"$TEST_TMP/registry.txt" \
  || fail "shipped registry validates"
grep -q 'registry ok' "$TEST_TMP/registry.txt" || fail "registry-validate output"
python3 "$ROOT/scripts/ssa/cli.py" workers >"$TEST_TMP/workers.txt" \
  || fail "workers lists"
for smoke_worker in codex grok kimi; do
  grep -q "^${smoke_worker}	" "$TEST_TMP/workers.txt" || \
    fail "workers lists $smoke_worker"
done
# Deleting the per-CLI case arms is the point of the registry. If one comes
# back, worker knowledge has started forking again.
if grep -q 'case "\$worker" in' "$SSA"; then
  fail "launcher has no per-CLI case arm"
fi
pass "registry is the single source of worker knowledge"

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

route_state="$TEST_TMP/route-state"
mkdir -p "$route_state"

XDG_STATE_HOME="$route_state" python3 - "$USAGE" <<'PY' || fail "effective headroom"
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("ai_cli_usage", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

# 90% of a 5h window spent, but it resets in 24 minutes: nearly free.
short = module.Window(
    name="5h_session", used_pct=90.0, remaining_pct=10.0,
    resets_in_hours=0.4, period_seconds=5 * 3600,
)
assert module.effective_score(short) > 85, module.effective_score(short)
assert module._effective_basis(short) == "short"

# Weekly windows are judged against pace, not against raw percent left.
week = 7 * 86400
ahead = module.Window(  # 30% used with 10% of the week gone: ahead of pace
    name="weekly_all", used_pct=30.0, resets_in_hours=0.9 * week / 3600.0,
    period_seconds=week,
)
behind = module.Window(  # 50% used with 80% of the week gone: comfortably behind
    name="weekly_all", used_pct=50.0, resets_in_hours=0.2 * week / 3600.0,
    period_seconds=week,
)
assert module.effective_score(behind) > module.effective_score(ahead)
assert module._effective_basis(behind) == "pace"

unknown = module.Window(name="mystery", used_pct=40.0, remaining_pct=60.0)
assert module.effective_score(unknown) == 60.0
assert module._effective_basis(unknown) == "raw"
PY
pass "effective headroom discounts resets and prices pace"

XDG_STATE_HOME="$route_state" python3 - "$USAGE" <<'PY' || fail "filter then rank"
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("ai_cli_usage", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def fleet(scores):
    return [
        module.CliStatus(
            cli=cli, available=True, eligible=True, score=v, effective_score=v
        )
        for cli, v in scores.items()
    ]


# The floor now applies at every size, not just medium and large.
thin = {cli: 10.0 for cli in module.WORKER_CLIS}
small_hard = module.recommend(fleet(thin), task_size="small", difficulty="hard")
assert small_hard["primary_worker"] is None, small_hard["primary_worker"]
assert "not dispatching on fumes" in " ".join(small_hard["reasons"])
small_routine = module.recommend(fleet(thin), task_size="small", difficulty="routine")
assert small_routine["primary_worker"] is not None
assert small_routine["floor_relaxed"] is True

# Same fleet, two objectives: hard buys quality, routine spends the cheapest
# headroom. kimi has the most room; codex has the best impl fit.
mixed = {"codex": 60.0, "grok": 50.0, "kimi": 90.0}
hard = module.recommend(fleet(mixed), task_size="medium", task_kind="impl",
                        difficulty="hard")
routine = module.recommend(fleet(mixed), task_size="medium", task_kind="impl",
                           difficulty="routine")
assert hard["rank_basis"] == "fit" and hard["primary_worker"] == "codex", hard
assert routine["rank_basis"] == "headroom" and routine["primary_worker"] == "kimi"
top = routine["ranked"][0]
assert {"score", "adjusted_score", "effective_score", "fit", "rank_basis"} <= set(top)

# A preferred CLI that survives the filter wins outright.
pref = module.recommend(fleet(mixed), task_size="medium", task_kind="impl",
                        difficulty="hard", prefer="grok")
assert pref["primary_worker"] == "grok", pref["primary_worker"]
PY
pass "recommend filters then ranks by difficulty objective"

XDG_STATE_HOME="$route_state" python3 - "$USAGE" <<'PY' || fail "admission floor"
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("ai_cli_usage", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

# Behind pace on a weekly window ranks well, but 7% left funds nothing.
starved = module.CliStatus(
    cli="codex", available=True, eligible=True, score=7.0,
    windows=[module.Window(
        name="primary_window", used_pct=93.0, remaining_pct=7.0,
        resets_in_hours=3.9 * 24, period_seconds=7 * 86400, severity="critical",
    )],
)
module.apply_effective(starved)
assert starved.effective_score > 21, starved.effective_score  # pace says fine
assert starved.admission_score == 7.0, starved.admission_score  # capacity says no

small_hard = module.recommend([starved], task_size="small", difficulty="hard")
assert small_hard["min_score"] == 21.0, small_hard["min_score"]
assert small_hard["primary_worker"] is None, small_hard["primary_worker"]

# A short window about to reset is real capacity, so it still gets admitted.
refilling = module.CliStatus(
    cli="grok", available=True, eligible=True, score=10.0,
    windows=[module.Window(
        name="5h_session", used_pct=90.0, remaining_pct=10.0,
        resets_in_hours=0.4, period_seconds=5 * 3600, severity="critical",
    )],
)
module.apply_effective(refilling)
assert refilling.admission_score > 85, refilling.admission_score
admitted = module.recommend([refilling], task_size="small", difficulty="hard")
assert admitted["primary_worker"] == "grok", admitted["primary_worker"]
assert admitted["ranked"][0]["admission_score"] > 85
PY
pass "admission reads capacity, not pace"

cooldown_state="$TEST_TMP/cooldown-state"
mkdir -p "$cooldown_state"
XDG_STATE_HOME="$cooldown_state" python3 - "$USAGE" <<'PY' || fail "cooldown gating"
import importlib.util
import json
import sys
import time

spec = importlib.util.spec_from_file_location("ai_cli_usage", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def fleet():
    return [
        module.CliStatus(cli=cli, available=True, eligible=True, score=90.0,
                         effective_score=90.0)
        for cli in module.WORKER_CLIS
    ]


module.set_cooldown("codex", "rate-limit")
rec = module.recommend(fleet(), task_size="medium", difficulty="routine")
assert rec["primary_worker"] != "codex", rec["primary_worker"]
assert "codex" in rec["cooldowns"]
assert any("cooldown (rate-limit" in r for r in rec["reasons"]), rec["reasons"]
assert "codex" not in {r["cli"] for r in rec["ranked"]}

path = module._cooldown_path()
data = json.loads(path.read_text())
data["codex"]["until"] = time.time() - 60
path.write_text(json.dumps(data))
rec = module.recommend(fleet(), task_size="medium", difficulty="routine")
assert rec["cooldowns"] == {}
assert "codex" in {r["cli"] for r in rec["ranked"]}

module.set_cooldown("grok", "auth")
assert module.load_cooldowns()["grok"]["minutes_left"] > 60
assert module.clear_cooldown("grok") is True
assert module.load_cooldowns() == {}
PY
pass "cooldowns bench a worker until they expire"

fit_state="$TEST_TMP/fit-state"
mkdir -p "$fit_state"
XDG_STATE_HOME="$fit_state" python3 - "$USAGE" <<'PY' || fail "learned fit"
import importlib.util
import json
import sys
import time

spec = importlib.util.spec_from_file_location("ai_cli_usage", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

ledger = module._ledger_path()
ledger.parent.mkdir(parents=True, exist_ok=True)


def write(n):
    now = time.time()
    lines = []
    for i in range(n):
        lines.append(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - i * 600)),
            "worker": "codex",
            "kind": "impl",
            "outcome": "verified-pass",
            "retries": 0,
        }))
    lines.append("{ this is not json")
    ledger.write_text("\n".join(lines) + "\n")


write(12)
value, n_eff, used = module.fit_posterior("codex", "impl")
assert used is True, (value, n_eff)
assert n_eff >= 10, n_eff
assert value <= 1.15 and value >= 0.85, value
rows, skipped = module.read_ledger()
assert skipped == 1, skipped

write(3)
value, n_eff, used = module.fit_posterior("codex", "impl")
assert used is False, (value, n_eff)
assert 0.85 <= value <= 1.15, value

rec = module.recommend(
    [module.CliStatus(cli=c, available=True, eligible=True, score=90.0,
                      effective_score=90.0) for c in module.WORKER_CLIS],
    task_size="medium", task_kind="impl", difficulty="hard",
)
assert set(rec["fit"]) == set(module.WORKER_CLIS)
assert {"prior", "posterior", "n_eff", "used"} <= set(rec["fit"]["codex"])
assert rec["fit"]["codex"]["used"] is False
PY
pass "learned fit stays advisory until it has samples"

forecast_state="$TEST_TMP/forecast-state"
mkdir -p "$forecast_state"
XDG_STATE_HOME="$forecast_state" python3 - "$USAGE" <<'PY' || fail "burn forecast"
import importlib.util
import json
import sys
import time

spec = importlib.util.spec_from_file_location("ai_cli_usage", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

now = time.time()
rows = [
    {"ts": now - 3600, "cli": "codex", "window": "thin", "used_pct": 10.0},
    {"ts": now - 1800, "cli": "codex", "window": "thin", "used_pct": 20.0},
]
for i, pct in enumerate([10.0, 20.0, 30.0, 40.0]):
    rows.append({"ts": now - (4 - i) * 1800, "cli": "grok", "window": "rising",
                 "used_pct": pct})
path = module._history_path()
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("".join(json.dumps(r) + "\n" for r in rows))

assert module.forecast("codex", "thin") is None
hours = module.forecast("grok", "rising")
assert hours is not None and hours > 0, hours

statuses = [module.CliStatus(
    cli="grok", available=True, eligible=True, score=60.0,
    windows=[module.Window(name="rising", used_pct=40.0, remaining_pct=60.0,
                           resets_in_hours=48.0, period_seconds=7 * 86400)],
)]
module.annotate_forecasts(statuses, now=now)
assert statuses[0].windows[0].forecast_exhausts_in_hours == hours
rec = module.recommend(statuses, task_size="medium", difficulty="routine")
assert any("at current burn" in r for r in rec["reasons"]), rec["reasons"]
assert rec["local_labor_ok"] is True
PY
pass "burn forecast stays advisory"

cool_repo="$TEST_TMP/cooldown-repo"
cool_task="$TEST_TMP/cooldown-task"
cool_dispatch_state="$TEST_TMP/cooldown-dispatch-state"
codex_429="$TEST_TMP/codex-429.sh"
make_repo "$cool_repo"
make_task "$cool_task" "$cool_repo"
cat >"$codex_429" <<'SH'
#!/bin/sh
echo "429 Too Many Requests"
exit 1
SH
chmod +x "$codex_429"
cool_rc=0
XDG_STATE_HOME="$cool_dispatch_state" CODEX_BIN="$codex_429" "$SSA" dispatch \
  --dir "$cool_task" --worker codex >"$TEST_TMP/cooldown-dispatch.txt" 2>&1 || cool_rc=$?
[[ "$cool_rc" == "1" ]] || fail "cooldown dispatch exit status"
grep -q 'cooldown=codex:rate-limit' "$TEST_TMP/cooldown-dispatch.txt" || \
  fail "dispatch reports the cooldown"
grep -q 'rate-limit' "$cool_dispatch_state/smart-subagents/cooldowns.json" || \
  fail "dispatch writes the cooldown state"
XDG_STATE_HOME="$cool_dispatch_state" "$SSA" cooldown --cli codex --clear \
  >"$TEST_TMP/cooldown-clear.txt" || fail "cooldown clear"
grep -q 'cooldown cleared' "$TEST_TMP/cooldown-clear.txt" || \
  fail "cooldown clear reports"
pass "dispatch benches a rate-limited worker"

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
