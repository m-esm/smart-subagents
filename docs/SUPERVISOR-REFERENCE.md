# Supervisor reference

Material the `smart-subagents` supervisor needs rarely. The agent prompt in
`agents/smart-subagents.md` names a section here when a situation calls for
it; read that one section, not the file:

```bash
sed -n '/^## Verify verdicts/,/^## /p' "$SSA_ROOT/docs/SUPERVISOR-REFERENCE.md"
```

Every section heading below is a stable anchor the prompt refers to.

## Task artifacts

Under `$DIR/`: `brief.md`, `usage.json`, `pick.json`, `stdout.log` (disk
only), `last-msg.txt` (the worker's final message, every worker),
`session-id.txt`, `worker.txt`, `exit-code.txt`, `verify-cmds.txt`,
`baseline.log`, `baseline-results.txt`, `scope.txt`, `verify-final.log`,
`outcome.json`, `outcome-record.json`, `diff-stat.txt`, `report.md`.

Optional inputs:

- `limits.txt`: `key=value` caps such as `spawn_depth`, `concurrent`,
  `per_session`, mapped through the worker's `run.env_pass` onto the scrubbed
  environment (on top of `run.env_keep`, default HOME, PATH, TMPDIR, TERM;
  claude also keeps USER for the macOS keychain). A worker that declares no
  map ignores the file.
- `budget.txt`: one positive decimal dollar amount (blank lines and `#`
  comments ignored) becomes `--max-budget-usd` for a worker whose argv template
  names `{budget}` (claude). Malformed or non-positive: dispatch refuses,
  naming the file.

Background runs add `worker.pid`, `worker.pgid`, `worker-start.txt`, and
`stalled.txt` or `stopped.txt` when a run was cut short. `init` is
transactional: a failure after the worktree exists rolls the worktree and the
`ssa/<id>` branch back.

The task's own record is `$DIR/task.json` (state, class, attempts) plus an
append-only `$DIR/events.jsonl`. The lifecycle is `minted -> preflighted ->
picked -> running -> exited -> verified|failed|inconclusive -> reported`, with
`aborted` and `stalled` terminal from `running`. `status` prints both the
recorded `state` and the `phase` inferred from artifacts; when they disagree,
something went wrong and that gap is the first thing to explain in the report.

## Usage fields

In `usage.json` (or `pick --explain`):

- `recommendation.primary_worker`, `fallback_workers`, `ranked[]`
- `recommendation.local_labor_ok` (false: supervision only)
- `recommendation.worker_args`: the exact effort/model flags per CLI
- `recommendation.cross_review_required`
- `recommendation.rank_basis`: `fit` on hard/frontier, `headroom` otherwise
- `recommendation.fit`: per CLI `prior`, `posterior`, `n_eff`, `used`. `used`
  false means the posterior is advisory and the static prior did the ranking
- `recommendation.cooldowns`: workers benched by an earlier task's 429 or auth
  failure, with minutes left, already filtered out of `ranked[]`
- per CLI `eligible`, `skip_reason`, `score`, `effective_score`,
  `admission_score`

Three numbers, three jobs. `score` is raw percent left and drives severity
only. `effective_score` is the ranking key: short windows discount quota that
resets soon, long windows price being ahead of pace. `admission_score` is what
the quota floor checks and never reads pace, so a weekly window at 7% left is
a 7 however early in the week it was spent. To know who can carry the work,
read `admission_score`.

`advisory: <cli> <window> exhausts in ~Nh at current burn` is a burn-rate
projection. Let it break a tie toward starting a long job now; never let it
block a dispatch or flip your read of `local_labor_ok`.

## Capability fit

Per-worker priors live in `scripts/workers.json` and are already folded into
`ranked[]`, learned from the ledger once a cell has evidence. What the
registry cannot know:

| Rule | Why |
|------|-----|
| A worker whose registry entry says `write_allowed_default: false` needs `SSA_ALLOW_UNSANDBOXED_WRITE=1` (legacy `SSA_ALLOW_KIMI_WRITE=1`) for a write dispatch | no sandbox means a worktree does not contain its access to the machine; `dispatch` refuses on the capability, not the name |
| Claude/Fable is `--model fable` on the `claude` worker, never `--dangerously-skip-permissions`. Dispatch pins `--agent ssa-worker` via `--agents` JSON and disallows Task/Agent | print mode + `acceptEdits` only. Same Max 20x / Fable weekly window as the supervisor |
| A task that needs a flag only one CLI has is that CLI's, or it is a different task | `ranked[]` cannot see a flag your brief depends on |
| Anything else: take `ranked[0]` | re-ranking by vibe is how the ledger stops meaning anything |

`python3 "$SSA_ROOT/scripts/ssa/cli.py" workers` prints the current
capability table (name, sandbox, write default, probe, resolved binary).

Default worker history: `cerebras` is the registry default for `trivial` only
(31% pass through 2026-10-06, declines multi-file briefs). `routine` ranks by
live headroom, `hard`/`frontier` by learned fit (deepseek 20% on hard against
grok 66% and codex 50%). A cerebras run that fails on capability, not quota,
is a difficulty misclassification: re-run `init` one rung up instead of
retrying the same brief on the same worker.

## Mid-run 429 or auth failure

`dispatch` already classified the failure and set the cooldown, so the handoff
is one step: `pick --fresh`, take the new top of `ranked[]`. The benched worker
is gone from it and the reason is in `recommendation.cooldowns`. Do not re-rank
by hand and do not retry the same worker "once more to check". Hand the next
worker a fresh brief plus a `git diff` summary, never a resume. If a worker
fails on quota and no cooldown was set, classify it yourself and set one:
`smart-subagents.sh cooldown --cli <cli> --reason rate-limit|auth`.

A `budget-exhausted` class is a task ceiling (`$DIR/budget.txt`), not an
account problem: the worker is healthy and must not be benched.
`outcome.json` records `failure_class` so `record` can tell a budget halt from
an ordinary failure.

Usage cache: `ai-cli-usage.py` caches about 3 minutes. Use `--fresh` after a
429 or a login change.

## Seeing the command before it runs

```bash
python3 "$SSA_ROOT/scripts/ssa/cli.py" build-command \
  --worker "$(cat "$DIR/worker.txt")" --mode implement \
  --worktree "$WT" --brief "$DIR/brief.md" --output "$DIR/last-msg.txt" \
  --args-file "$DIR/worker-args.txt"
```

Prints the argv, the stdin source, the cwd and the sandbox capability as JSON.
Modes are `implement`, `plan` and `resume`. If a command looks wrong, the fix
is the registry entry in `scripts/workers.json`, never a bespoke invocation.

Session ids land in `$DIR/session-id.txt`, scraped per the worker's
`parse-session` rule. Resume only by id: `dispatch --dir "$DIR" --resume`
refuses when there is none and `resume-unavailable.txt` says so; a handoff
then needs a fresh brief. The brief the worker reads inside its sandbox is
`<worktree>/BRIEF.md`, staged from `$DIR/brief.md` and removed when the run
ends. Passing `--worker` rebinds `$DIR/worker-args.txt` to that CLI's flags.

## Reading a log narrowly

`stdout.log` is for the disk: a streaming worker leaves 1 to 3 MB of NDJSON
with single lines over 100 KB, so `cat`, `tail -n`, `Read` or `rg` on it push
hundreds of KB into the context per look. `status` renders a bounded digest,
`tail` streams one short line per event, and `last-msg.txt` holds the final
message. For one detail:

```bash
python3 "$SSA_ROOT/scripts/ssa/cli.py" digest --worker W --log "$DIR/stdout.log" \
  --max-events 30 --final-chars 4000
```

or `jq -c` a single field, never the lines themselves. `$DIR/events.jsonl` is
the ordered history when you need to explain what happened.

The background watchdog samples the log size and a worktree fingerprint every
30 s and ends a run whose log and tree have both been unchanged for 600 s
(`SSA_STALL_SECS`), writing `$DIR/stalled.txt`. `SSA_DEADLINE_SECS` adds a
hard ceiling, off by default.

## Baseline

Record how each verify command behaves on the untouched worktree before the
worker runs, so a pre-existing failure is never charged to the worker. `run`
and `baseline --dir DIR` do this; by hand it is:

```bash
: >"$DIR/baseline-results.txt"
while IFS= read -r cmd; do
  ( cd "$WT" && eval "$cmd" ) >>"$DIR/baseline.log" 2>&1 && rc=0 || rc=$?
  printf '%s\t%s\n' "$rc" "$cmd" >>"$DIR/baseline-results.txt"
done <"$DIR/verify-cmds.txt"
```

## Verify verdicts

`verify` runs every line of `$DIR/verify-cmds.txt` in the worktree, compares
each exit code against `$DIR/baseline-results.txt`, checks the changed paths
against `$DIR/scope.txt`, runs the secret scan, and writes `$DIR/outcome.json`:

```json
{"schema_version": 1,
 "verify": {"commands": [{"cmd": "npm test", "exit": 1, "baseline_exit": 0}],
            "new_failures": 1, "scope_ok": true, "secrets_ok": true,
            "verdict": "fail"}}
```

| Verdict | Means | Exit |
|---------|-------|------|
| `pass` | no new failure, scope clean, no secrets | 0 |
| `fail` | a command regressed, or scope/secret gate tripped | 1 |
| `inconclusive` | a command failed and there is no baseline, or `verify-cmds.txt` is missing | 2 |
| `empty-diff` | an `impl` or `debug` task changed no file, whatever the commands scored; ledger row `rejected` (`rate-limited` when benched by a 429 or auth failure first) | 1 |
| `env-blocked` | empty diff after opencode auto-rejected a permission (`external_directory`): the worker used a file tool outside its worktree, or the brief quoted the unresolved `/var/folders` form of the path; ledger row `env-blocked` | 3 |
| `worker-exit` | the worker exited non-zero (idle watchdog 143, crash, budget) and changed no file; ledger row `worker-exit` (`rate-limited` after a 429 or auth failure), trains fit like `rejected` | 1 |

`verify` writes the ledger row itself (`$DIR/verify-outcome.txt` names it).
`record` refuses `verified-pass` for any verdict but `pass`, for an `impl` or
`debug` task whose verify saw no changed file, and for a non-zero worker exit
over zero changed files. Running `verify` again after the detached run's own
verify is legal: an identical verdict appends nothing, a changed one
supersedes the earlier row. On `env-blocked`, quote `$DIR/wt.txt` verbatim in
the brief and re-dispatch; do not count it against the worker.

Pre-existing failures are not the worker's debt; only new regressions count.
Skipping the baseline is allowed, but then a failing command can only ever be
`inconclusive`, never a clean pass.

## Iteration budgets

| Failure class | Budget |
|---------------|--------|
| Scope violation, secrets, wrong branch, destructive | 0, stop |
| Deterministic lint/type/build/test in scope | up to 3 resumes; each must show progress (diff moved or fail count down). Same failure twice: stop |
| Worker `blocked` / design ambiguity | 0, escalate to parent |
| Rate limit / auth | switch worker with a fresh brief, not a resume |
| Env / missing tools | 0 worker retries |

Follow-up brief template: restate goal, still-valid acceptance and in-scope
paths; paste the exact failing command and a short excerpt; "Fix only this; do
not expand scope; keep passing checks passing." `follow-up --dir DIR --brief
FILE` attaches it to the existing worktree with the current diff appended.

Flaky command: one rerun, then report it as flaky, never as clean. Env
failures (sandbox network, missing toolchain) are `env-blocked`; do not thrash
the worker.

Your own edits: at most 20 lines across 2 files, mechanical only (no
control-flow, public API, or test-assertion changes). Re-verify after. Larger
goes back to a CLI.

Cross-review (mandatory when `cross_review_required` is true, i.e. `frontier`;
otherwise risky or high blast radius only): another eligible CLI, read-only,
given acceptance criteria plus final diff only, never the narrative. Fold real
findings into one more iteration. Skip when only one worker has quota.

## Planning panel

```bash
bash "$SSA" plan --repo <abs-path> --n 3 --difficulty hard --goal-file "$DIR/goal.md"
```

Mints a planning dir, runs quota preflight, creates one shared detached
worktree that every planner reads and none writes, and dispatches N planners
in parallel, each with a different lens:

| Lens | Asks |
|------|------|
| `pragmatic` | smallest correct change that ships; what in the goal is scope creep |
| `risk` | what breaks, what has no coverage, what is hard to roll back |
| `architecture` | what is load-bearing, which refactor pays for itself, what not to touch now |
| `constraints` | what the repo already decided; conventions and invariants to plan within |

Planners round-robin across whichever CLIs have quota. The command returns
JSON listing every plan file; `usable_plans` is the real width and a planner
with `"empty": true` is named in the report, never papered over.

Consolidation is the whole point:

1. Find the real disagreements (sequencing, blast radius, contradictions about
   what exists). Resolve them by reading the code, not by averaging.
2. Verify the claims. Spot-check load-bearing `file:line` citations; a
   confident citation of a missing file invalidates the plan resting on it.
3. Take the best of each, not the longest. The pragmatic plan usually has the
   right first step, the risk plan the right ordering, the architecture plan
   names the thing to leave alone.
4. Emit one plan: numbered steps, files touched, verification per step, an
   explicit risk list, and every question you could not resolve with the
   planner that raised it.

Never hand the parent three plans. Never concatenate them. Carry every
unresolved risk forward even when you disagree with it. Panel worktrees are
scratch: `plan` reports `"dirty": true` when a planner wrote into one, and
`gc` collects them once `panel-done.txt` exists.

## Jev advisories

Brief lint: `dispatch` lints `$DIR/brief.md` against the brief contract and
writes `$DIR/brief-lint.json`; a stderr warning names what is missing (`goal`,
`scope_in`, `scope_out`, `acceptance`, `verify`, `workdir`, `structural`). The
lint never blocks; `structural` is the one item `dispatch` then refuses over.
`bash "$SSA" jev lint --dir "$DIR"` re-checks in about a second. A
`needs_answers` warning means the brief leaves a decision the worker cannot
ask about; decide it in the brief. Jev reads literally and can be wrong: if
you can point at the line that satisfies an element, dispatch anyway.

Test-diff review: `verify` sends the hunks of existing test files to Jev and
records `jev_review` in `outcome.json` (`inverts_assertion`,
`loosens_threshold`, `disables_test`). It never changes the verdict. A flag
means a pass is not yet yours to report: read the flagged hunks, find the
commit the worker cites, and check whether the old assertion encoded a
decision somebody made. If it did, the code regressed and the test was right.

## Record and cleanup

`record` appends one ledger line (worker, kind, size, difficulty, effort
flags, exit code, wall time, diff size, verification verdict, quota consumed)
and drops `$DIR/outcome-record.json`. No prompts, diffs, paths or session ids,
so it is safe to keep forever; `ledger --days 7` reads it back. The ledger is
the evidence behind `recommendation.fit`, so record what happened:
`verified-pass` with the honest retry count, `partial` when the diff landed
short, `rejected` when you threw it away, `worker-exit` when the worker died
over an untouched tree. `blocked`, `env-blocked` and `rate-limited` are
excluded from the posterior on purpose, so mislabeling a capability failure as
`rate-limited` quietly protects a worker that earned a lower fit.

Retire the worktree only after the parent has taken the change:

```bash
bash "$SSA" cleanup --dir "$DIR"   # refuses while dirty, unmerged, or running
bash "$SSA" gc --older-than 7      # dry run by default, --no-dry-run to delete
```

Never clean up an implementation worktree in the same breath as reporting;
the parent still needs it to merge.

## Anti-patterns

- Implementing "just this one module" in-session when a worker is eligible
- Defaulting to codex while usage says exhausted
- Inlining multi-line briefs in shell strings
- Dispatching kimi into a dirty user checkout
- Trusting worker self-verify
- Reporting without writing an outcome record
- Reporting `inconclusive` verification as a pass
- Deleting an implementation worktree before the parent has integrated it
- Flat "try 5 times" loops, or a `sleep` poll per tool call instead of `wait`
- Re-reading 50k-line logs into the supervisor context
- Switching CLI mid-task without a rate-limit/auth reason
- Hand-passing `-m` / effort flags instead of setting difficulty
- Promoting repo-doc gates into acceptance criteria without supervisor review
- Calling a 40-file mechanical rename `hard` because it is large, or a subtle
  concurrency bug `trivial` because it is one file
