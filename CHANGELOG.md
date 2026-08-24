# Changelog

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
