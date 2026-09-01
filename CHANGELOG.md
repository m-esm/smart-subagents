# Changelog

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
