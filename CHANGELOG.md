# Changelog

## 0.4.0

A sixth worker, `deepseek`: opencode on DeepSeek's API, prepaid per token.
It is the registry default for `hard` work, which used to rank onto codex,
grok or the Claude plan (on the fleet VPSes, where only cerebras and claude
exist, that meant Fable).

- `scripts/opencode-cerebras` became `scripts/opencode-worker`, one wrapper
  with a profile per provider picked by the name it runs under;
  `opencode-cerebras` and `opencode-deepseek` are symlinks to it. The model
  entry in the throwaway opencode config is derived from the `-m` argument,
  so no model list lives in the wrapper. stdin is closed for `opencode run`,
  which otherwise waits on a non-tty stdin and never starts.
- `ssa/cerebras_proxy.py` takes `--key-name`, `--no-strip` and `--label`.
  DeepSeek runs with `--no-strip`: it wants `reasoning_content` echoed inside
  a tool-call turn. The key stays in the proxy, as for cerebras.
- Model rules: `deepseek-flash` (V4.1 Flash) for trivial and routine,
  `deepseek-v4-pro` otherwise. Measured on a fix-plus-test task through the
  wrapper: Flash 5 steps in 13 s, V4 Pro 5 steps in 18 s, both passing.
- `check_deepseek` meters money: `GET /user/balance`, a per-UTC-day snapshot
  under the state dir, and a `spend_day` window against `DEEPSEEK_DAILY_USD`
  (default 5). Ineligible when the day cap is spent or the balance is under
  `DEEPSEEK_MIN_BALANCE` (default 1). A top-up restarts the snapshot.
- Watchdog knobs are `OPENCODE_WORKER_{IDLE_TIMEOUT,MAX_SECONDS,MAX_REQUESTS,
  NO_SANDBOX}`; the `OPENCODE_CEREBRAS_*` names still work.

## 0.3.9

A fifth worker, `cerebras`: opencode on Cerebras' API (gpt-oss-120b, about
3000 tokens/s, a 720M-token day window on the measured account), and the
first worker the registry can make the default.

- `scripts/opencode-cerebras` drives `opencode run` through
  `scripts/ssa/cerebras_proxy.py`, a stdlib reverse proxy on 127.0.0.1 that
  strips the `reasoning_content` echo Cerebras answers with a 400 (every
  AI-SDK client sends it, so the second turn of any agentic run died) and
  injects the key from `~/.config/cerebras/env`, so the worker process never
  holds it. On macOS `sandbox-exec` confines writes to the worktree, TMPDIR
  and opencode's state dirs; without it the wrapper refuses unless
  `OPENCODE_CEREBRAS_NO_SANDBOX=1`. Measured: a two-file task with tests in
  13 s, 4 steps.
- The wrapper rewrites the worktree path in every argument to its real path
  (a `$TMPDIR` worktree arrives symlinked and double-slashed, and opencode
  auto-rejects any path not spelled under its resolved project root as an
  "external directory"), and runs a watchdog: no LLM request through the
  proxy for `OPENCODE_CEREBRAS_IDLE_TIMEOUT` seconds (300) or
  `OPENCODE_CEREBRAS_MAX_SECONDS` (1800) of wall time kills opencode, which
  was seen to sit idle after its loop while the dispatcher has no cap;
  `OPENCODE_CEREBRAS_MAX_REQUESTS` (120) kills a model that keeps calling
  tools without converging.
- Measured on a two-file task with tests: gpt-oss-120b 5 of 5 runs green in
  12 to 30 s; qwen-3.8-27b looped past 42 steps on the first run, so the
  registry pins gpt-oss-120b for every difficulty.
- `adapters.launched_model` reads the registry's own model flag (`-m`), so
  `model-used.txt` is filled for kimi and cerebras, not only `--model`.
- `check_cerebras` reads the live meters from the rate-limit response
  headers of one 1-token completion (Cerebras has no usage endpoint); six
  windows, `tokens_day` binding, any exhausted window blocks eligibility.
- Registry: a relative binary candidate resolves against the registry's own
  directory; `default_for` (`{difficulty, size}`) names the worker
  `recommend()` makes primary when it survives the filter. Never on a relaxed
  floor, and `--prefer` still wins. `registry_default` is in the JSON.
- `adapters._error_text` reads a nested `error.data.message` (opencode's
  APIError shape), so its 429s reach the cooldown classifier.
- Codex 0.153 dropped `wire_api = "chat"` and Cerebras serves no Responses
  API, so codex cannot be the Cerebras driver; that is why opencode.

## 0.3.8

`claim_not_in_diff` flagged 4 of the first 5 real reviews at 0.94+, all false:

- The claim question shared `diff` with the test questions, so it saw only
  test hunks (or a diff clipped at 24K chars) and every source change the
  report named looked absent. It now reads `changed_files`: every path in the
  diff plus untracked files (`jev review --files LIST`, verify passes it).
  Replayed on those runs: 0.94/0.96/0.94/0.94 became 0.20/0.15/0.06/skipped;
  a fabricated file claim still scores 0.94.
- Commands run and gitignored build outputs named in a report are not claims.
- Above 400 changed files the question is skipped with a warning.
- `dismisses_encoded_decision` is only asked when a test hunk exists.
- The ledger row carries the review scores (`jev.review_scores`) beside the
  flags, so `REVIEW_THRESHOLD` can be tuned after `$TMPDIR` is cleaned.

Also in 0.3.8:

- Scope-check untracked, non-ignored worker files during verify, excluding SSA launch artifacts.
- Persist task-linked Jev decisions without brief, diff or report text in an
  append-only log (`SSA_JEV_DECISIONS`). Logging fails open.
- Include Jev labels/findings and optional parent/slice ids in outcome records.
- Add `jev tune [--days N] [--json]` for aggregate outcome joins (30 days by
  default), and regressions for `init --brief` and explicit-axis precedence.
- All Jev judgments remain advisory; thresholds and verification are unchanged.

## 0.3.7

Hot-path Jev, the bits that actually steer money after 0.3.6 named them:

- `classify` `flags` (and `init --brief`) downshift a low-confidence
  difficulty or size. `hard` at 0.72 still logs as hard; dispatch uses
  `effective.difficulty` (routine) unless `--difficulty` was passed.
- `jev review --report last-msg.txt` (verify does this) asks whether the
  worker dismissed a user-encoded test or claimed a change the diff lacks.
  Still advisory; verdict untouched.

## 0.3.6

Three gaps measured on one real dispatch (six red tests, verified-pass):

- `verify` now asks Jev whether the worker bent EXISTING tests: flipped an
  assertion, loosened a numeric check, or disabled a test. Only test-file
  hunks that lost a line are sent. Result in `diff-review.json` and
  `outcome.json` `jev_review`, warning on stderr, verdict untouched. The run
  that prompted it passed verify while turning `assertNotIn` into `assertIn`
  on a test that encoded a user decision. `jev review --dir|--brief` runs it
  by hand.
- `jev lint` reports a missing `## Structural discovery` section as
  `structural`, checked in code. Before this a brief scored 0.97+ on every
  element and `dispatch` refused it one step later.
- `difficulty` gets its own low-confidence bar (0.8, the rest stay 0.5) and
  `runner_up` names the rival level. `hard` at 0.72 had raised the quota floor
  to "no eligible worker" unflagged while `routine` dispatched.
- One retry on 503 as well as 429/529.

## 0.3.5

- New `jev classify|lint|preflight|probe`: advisory typed judgments about a
  brief from TypeSafe's Jev model (`scripts/ssa/jev.py`, stdlib only). classify
  returns size, difficulty and kind with a confidence each plus the flags line
  for `init`/`pick`; lint checks the brief contract and names what is missing.
  Of three past briefs with a recorded class it matched two exactly (the third
  read medium/hard against a recorded large/routine) and it filled `kind`,
  which all three had left at `default`.
- `dispatch` lints the brief before a fresh run, writes `brief-lint.json` and
  warns on stderr. It never blocks, and a resume is not linted.
- Everything fails open: no key, no network, a 4xx/5xx, a timeout or `SSA_JEV=0`
  exits 2 and changes nothing. One retry on 429/529. `doctor` reports whether a
  key is present (existence only).
- Option keys are derived from `BASE_FLOOR`, `DIFFICULTY` and `FIT`;
  `tests/test_jev.py` is the drift gate and runs against a local fake endpoint,
  so the suite never reaches TypeSafe (`SSA_JEV=0` in the test env).

## 0.3.4

Five-lens audit of SSA (context bounds, process lifecycle, log parsing,
routing, security and docs), every finding reproduced before it was fixed.

- Output every command prints is bounded: `verify-summary` clips each section
  to 200 lines and spills the rest to `verify-summary-full.txt` (measured 203
  KB before), `ls` shows the 20 most recent plus everything in flight (`--all`,
  `--state`), `gc` summarizes kept dirs by reason (`--verbose` restores the
  lines), `pick` puts one line on stderr (`--explain` for the JSON), and
  `outcome.json` carries 25 out-of-scope paths plus a count, with the full list
  in `verify-out-of-scope.txt`.
- New `diff --dir DIR [--path P] [--max-bytes N]`: the change as a stat, and
  one path's unified diff clipped, so the playbook never asks for a bare
  `git diff`.
- `dispatch --resume` continues the recorded session id, the mode the agent
  playbook already described.
- The background watchdog works again: a stale `exit-code.txt` no longer
  disarms it on the first tick, and it kills the worker's process group rather
  than the wrapper's, so a stalled run still writes its exit code, diff stat
  and final message. A foreground dispatch now records the worker's pid, pgid
  and start time, so `stop`, `gc` and `status` can see it.
- The staged `BRIEF.md` is excluded through the worktree's common git dir
  (`--absolute-git-dir` pointed at a per-worktree dir git never reads) and
  removed when the run ends, so it can no longer show as untracked, block
  cleanup, or be swept into a `git add -A`.
- Task state follows the task: `exited -> running` (re-dispatch), plus edges
  out of `stalled` and `aborted`. A refused transition is recorded in
  `state-desync.txt` and forced onto the record instead of being swallowed.
- Worktrees moved to `$SSA_WORK_DIR/wt/<task id>`, beside the task dir rather
  than inside it: a worker's cwd can no longer reach `../verify-cmds.txt` or
  `../scope.txt`. The work dir must be owned by the current user.
- The secret scan reads untracked files (where a leaked credential actually
  lands), drops the entropy threshold to 3.5 for 32+ character tokens (a
  40-char hex token measures 3.84), and records `gitleaks: ran|absent`.
- Planning panels are checked for writes after the run (`panel-dirty.txt`,
  `"dirty": true`), report `panel-done.txt` so `gc` cannot delete a live
  panel's worktree, roll their worktree back if `plan` dies, and give an empty
  planner a digest instead of a path to 555 KB of NDJSON.
- `doctor` prints `env_scrub` per worker from the registry and collapses
  `$HOME` to `~` in credential paths.

Routing, classification and digest fixes from the 2026-09-01 audit:

- Grok billing with `monthlyLimit.val = 0` and `used.val = 0` is an unreadable
  meter, not 100% headroom. It scored grok at full capacity and sent 85 of 106
  dispatches there. Same zero guard kimi already had.
- A failed usage probe names itself: `skip_reason` becomes "usage probe
  rate-limited" (429), "usage probe unauthorized" (401/403) or "usage probe
  failed (HTTP n)", so `recommend` says why a CLI dropped out.
- `local_labor_ok` fails closed. An empty claude `extras` (probe errored, or
  claude was never probed) read as permission; it now needs an available
  claude status that actually says `local_labor: true`, with a reason on the
  recommendation when it does not.
- Failure classification reads error envelopes, not the raw tail. A bare `429`
  matched UUID segments and grep hits like `test_lattices.py:429:`, and a bare
  `401` matched agent prose: 14 of 60 real logs misclassified, benching a
  healthy CLI for 15 min or 24 h. Both numbers now need a status word in
  front. Codex's real quota event (`turn.failed` with "You've hit your usage
  limit") and kimi's `resource_exhausted` frame are recognized at last.
- A stale `refresh.lock` left by SIGKILL (one was 8 days old) is broken after
  one cache TTL instead of costing every cold-cache caller a 10 s wait.
- Optional per-worker `error` rule in `workers.json`, validated like `final`.
  A run that ended on `turn.failed` or an error result reports
  `[run failed: ...]` instead of the last successful message, and the digest
  carries a `terminal` field.
- Digests keep the last 3 non-JSON lines as `stderr`: a crash whose stack
  trace went to the merged fd used to digest to nothing.
- Kimi's `final` rule targets its assistant lines. Its `session.resume_hint`
  meta line comes last, so the final message was the resume hint. Digests also
  summarize kimi's `tool_calls` and tool results.
- Session scraping and final messages share one JSON ladder: one trailing
  stderr line after claude's object no longer loses the session id, and a
  pretty-printed object still yields a final message.
- `format` values in the registry are validated (`jsonl`, `json`, `text`).
- Security: claude drops `--setting-sources project` (it loaded the target
  repo's `.claude/settings.json`, whose hooks then ran unsandboxed on this
  machine) and runs with `env_scrub`, keeping HOME, PATH, TMPDIR and TERM,
  which is all `claude -p` needs to find its own credentials.
- Security: grok takes the brief by file reference like kimi and claude. The
  whole brief in argv was readable by every local user through `ps`.
- New `tests/test_docs_tables.py` gates the hand-written difficulty and
  quota-floor tables in the docs against `DIFFICULTY` / `BASE_FLOOR`.

## 0.3.3

Worker logs no longer leak into the supervisor context. Measured on
2026-09-01: grok implement runs left 1-3 MB of NDJSON with single lines up to
186 KB, and `status` piped the last three raw lines into every poll.

- New `ssa/digest.py` plus CLI `digest`, `final-message`, `tail-filter`: a
  byte-capped view of any worker log (event counts, last events clipped, final
  message clipped), shaped by a per-worker `final` rule in `workers.json`.
- `status` prints that digest instead of raw lines (3 MB log: 2.4 KB output).
- `tail --dir` streams one short line per event; `--raw` is the old firehose.
- `dispatch` writes `last-msg.txt` for every worker (grok, kimi and claude have
  no `-o` flag) and ends with a clipped final message.
- Grok drops `--include-partial-messages`: per-token deltas were 43% of the
  log and the watchdog only needs per-turn growth.

## 0.3.2

Grok usage probe refreshes the x.ai OAuth token before billing calls, and
retries once on 401. A stale bearer no longer marks a working CLI ineligible.
`--fresh` no longer serves a cached 401 when the refresh lock is held.

## 0.3.1

Implement dispatch requires a Structural discovery section: `CGC:` pack path
or `CGC-SKIP` with route and evidence. A NOT IN GRAPH stub is not a pack.
Ledger records `route`. Rollback: `SSA_STRUCTURAL_LEGACY=1`.

## 0.3.0

Claude Code is a shipped worker, pinned to Fable.

- Registry entry `claude`: `claude -p --model fable`, print mode, `--permission-mode acceptEdits` (plan mode for `plan`). Never bypassPermissions.
- Reuses the existing `check_claude` probe, so Fable weekly and the 5h session meter the worker and `local_labor_ok` together.
- No sandbox, write-blocked by default. Override is `SSA_ALLOW_UNSANDBOXED_WRITE=1`; `SSA_ALLOW_KIMI_WRITE=1` remains an alias.
- Fit prior is high for impl/review (1.15) and low for best-of-n (0.85), so hard work can take Fable and fan-out does not.

## 0.2.1

Docs: rewrite the mermaid charts so they read as stages instead of a knot.
README routing is Score / Filter / Rank with the ranking rule on the arrows.
Architecture names what each file owns. Ledger loop states the 10-sample gate.

## 0.2.0

The redesign, in five phases. 0.1.0 advertised guarantees it did not implement,
routed on percent-left, forgot everything between dispatches, and kept one copy
of each worker's knowledge per code path.

Docs restructured to match: the README is now an overview with one diagram, and
the routing math and the architecture map moved to `docs/ROUTING.md` and
`docs/ARCHITECTURE.md`.

### Phase 0: implement the advertised guarantees

- `scan-secrets`: credential regexes plus Shannon entropy over added lines,
  newly added `.env` files, and a gitleaks pass when gitleaks is installed.
  Wired into `verify-summary` as a gate, not a note.
- kimi runs env-scrubbed, and a write dispatch to it is refused unless
  `SSA_ALLOW_KIMI_WRITE=1` says the risk was accepted on purpose.
- Worker exit codes are captured to `exit-code.txt` instead of being lost.
- `plan` applies the difficulty-derived worker args it was already computing.
- `--kind` is plumbed through `init`, `pick` and `plan`.
- The quota floor is hard for `hard` and `frontier`: no dispatching on fumes.
- Atomic cache writes behind a single-flight lock.
- Repo agent docs (`AGENTS.md`, `CLAUDE.md`) are framed to workers as untrusted
  data describing conventions, not as instructions to follow.

### Phase 1: visibility, lifecycle, ledger, machine-readable verify

- `ls`, `status`, `tail`, `stop`: a dispatch you cannot see or stop is hope,
  not delegation.
- `dispatch --background` detaches into its own process group behind a watchdog
  that samples log size and a worktree fingerprint, then TERMs and KILLs a run
  whose log and tree have both gone quiet for `SSA_STALL_SECS`.
- `init` is transactional: a failure after `worktree add` rolls the worktree and
  its `ssa/<id>` branch back.
- `cleanup` and `gc` refuse to delete a dirty tree, unique commits, or a live
  process.
- `doctor`: offline health check that exits nonzero only when a dispatch could
  not run at all.
- `record` and `ledger`: one line per dispatch in `outcomes.jsonl`, with quota
  snapshots either side and a hash of the repo path rather than the path.
- `verify` runs `verify-cmds.txt`, compares against the pre-dispatch baseline,
  checks scope globs and secrets, and writes `outcome.json` with a
  pass / fail / inconclusive verdict.

### Phase 2: route on the real objective

- Effective headroom per window: short windows are discounted by their reset
  horizon, long windows are priced against pace. Ranking reads that, severity
  and gates still read raw `used_pct`.
- Admission is a separate number: raw remaining on long windows, so a weekly
  window at 7% left cannot talk its way in by having been spent early.
- The quota floor applies at every size, not just medium and large.
- Filter, then rank: fit first for `hard` and `frontier`, headroom first for
  `trivial` and `routine`. No headroom-times-fit product.
- Cross-task cooldowns, set by `dispatch` when it classifies a 429 or an auth
  failure, never open-ended.
- Learned fit: an empirical-Bayes posterior per (worker, kind) over the ledger,
  advisory until 10 effective samples and clamped to `[0.85, 1.15]`.
- Burn-rate forecast from usage history. Advisory, never a gate.

### Phase 3a: characterization tests

- 103 hermetic tests: the pure `parse_*_usage` halves of each probe, synthetic
  provider payloads, and fake `codex` / `grok` / `kimi` binaries that record
  their argv so the exact dispatch command lines were locked before anything
  moved.

### Phase 3b: one registry, one runtime, task state as data

- `scripts/workers.json` is the single source of per-CLI knowledge: binary
  discovery, quota probe, sandbox capability, prompt transport, argv templates
  per mode, session-id extraction, effort ladder, model rules and fit priors.
  Adding a fourth worker is an entry plus a probe, proven by
  `tests/test_registry.py`, which registers one through `SSA_WORKERS_JSON` and
  never edits a line of Python.
- `scripts/ssa/`: `registry.py` (load and validate, failing closed on unknown
  placeholders and shell metacharacters), `adapters.py` (build a command, scrape
  a session id, classify a failure), `state.py` (task.json, events.jsonl, an
  explicit transition table), `cli.py` (the seam the shell calls).
- The launcher lost its three per-CLI `case` arms and its three embedded
  session-id parsers. It keeps the lifecycle commands.
- Every task dir now carries `task.json` (state, class, attempts) and an
  append-only `events.jsonl`. `status` and `ls` report the recorded state and
  fall back to artifact inference for older task dirs.
- Routing reads the registry: `WORKER_CLIS`, the effort ladders, the fit priors
  and the difficulty-to-flag mapping all come from it. A registered worker whose
  probe does not exist is reported unavailable with `no quota probe`, never
  given invented headroom.
- Parser consistency, on purpose: missing usage data now means
  `available, not eligible, "usage data missing"` for every provider, where
  codex used to assume the worst and grok and kimi assumed the best. A malformed
  number drops that field to `None` and records a warning in `extras.warnings`
  instead of raising out of the parser.
- codex session-id extraction no longer accepts a bare top-level `id`. That key
  appears on ordinary stream items, so it produced unresumable garbage.

## 0.1.0

Quota-aware subagent routing: live usage per CLI, a difficulty axis separate
from size, worktree isolation, and the planning panel.
