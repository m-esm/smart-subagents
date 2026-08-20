# smart-subagents

Quota-aware subagent routing for Claude Code.

You pay for several AI coding CLIs. They all have separate, invisible rate limits.
Claude Code doesn't know about any of them, so it burns your most expensive model
on mechanical work while a subscription you already paid for sits idle with 100%
headroom.

This plugin adds a supervisor agent that reads the live quota on every CLI you're
logged into, picks the one with the most headroom and the right capability fit,
runs the work in an isolated git worktree, and verifies the diff itself before
reporting back. It never trusts a worker's self-assessment.

```
$ ai-cli-usage.py

[OK]   CLAUDE  plan=claude_max                 score=18
  (LOW) 5h_session: 82% used, 18% left, resets in 0.6h
[SKIP] CODEX   plan=plus                       score=0
  skip: Codex rate limit exhausted
[OK]   GROK    plan=SUBSCRIPTION_TIER_SUPER_GROK_PRO  score=100
[SKIP] KIMI                                    score=0

RECOMMENDATION
  primary_worker : grok
  local_labor_ok : true
```

## What it does

**Routes by live quota, not by habit.** Every CLI is scored 0-100 on remaining
headroom, read from that CLI's own usage API using its own stored credentials.
Exhausted CLIs are filtered out before ranking. A `large` task won't dispatch to
a CLI sitting at 30% headroom.

**Routes by capability fit.** Headroom is multiplied by a per-task-kind prior, so
`review` work leans toward a different CLI than `best_of_n` work, but only as a
tiebreak between CLIs that both have quota.

**Isolates every write.** Each task gets its own git worktree on an `ssa/<id>`
branch. Your checkout is never touched, dirty or not. Kimi has no sandbox at all,
so the worktree is the only thing standing between it and your working tree.

**Verifies instead of trusting.** The supervisor runs a baseline of your verify
commands before dispatch, so pre-existing failures aren't charged to the worker.
After dispatch it re-runs them itself, diffs against the recorded base SHA, checks
that changed paths stay in scope, and rejects on secrets, surprise lockfile bumps,
or an empty diff behind a "done" claim.

**Budgets its retries by failure class.** A deterministic test failure gets up to
3 resumes and each one must show progress. A scope violation or a leaked secret
gets zero. A rate limit switches worker with a fresh brief rather than retrying.

## Requirements

Python 3.9+, git, and at least one of:

| Worker | CLI | Write sandbox |
|---|---|---|
| codex | [OpenAI Codex CLI](https://github.com/openai/codex) | `-s workspace-write` |
| grok | Grok CLI | always `--sandbox workspace` |
| kimi | Kimi Code | none, worktree only |

You need to be logged into each CLI you want used. Nothing here handles auth; it
reads whatever credentials those CLIs already stored.

## Install

As a Claude Code plugin:

```
/plugin marketplace add m-esm/smart-subagents
/plugin install smart-subagents@smart-subagents
```

Or clone and point at it directly:

```bash
git clone https://github.com/m-esm/smart-subagents.git
ln -s "$PWD/smart-subagents/agents/smart-subagents.md" ~/.claude/agents/
export SSA_USAGE_PY="$PWD/smart-subagents/scripts/ai-cli-usage.py"
```

## Use

From Claude Code, delegate a task to the agent:

> Use the smart-subagents agent to port the auth middleware in ~/code/api from
> Express to NestJS. Acceptance: `npm test` passes and `npm run build` succeeds.

The agent needs four things in the brief and will refuse to guess at any of them:
absolute repo path, what's in and out of scope, checkable acceptance criteria,
and the exact verify commands.

Standalone, without the agent:

```bash
# Which CLI should take a medium implementation task?
ai-cli-usage.py --recommend --task-size medium --task-kind impl

# Full picture, machine-readable
ai-cli-usage.py --json --task-size large

# Mint a task: private work dir + isolated worktree + worker pick, in one call
smart-subagents.sh init --repo ~/code/api --size medium

# Write $DIR/brief.md, then:
smart-subagents.sh dispatch --dir "$DIR"
smart-subagents.sh verify-summary --dir "$DIR"
```

## Privacy

The usage checker reads local credential files to call each provider's usage
endpoint. It never prints, logs, or transmits a token.

Account emails are **redacted by default** in every output path, including
`--json` and the on-disk cache (`mo***@gmail.com`). Pass `--include-account` if
you actually want them. The cache lives at `$XDG_CACHE_HOME/smart-subagents/`
(mode 0600 in a 0700 directory), never in world-readable `/tmp`.

Task directories hold your brief, your worker logs, and a full worktree of your
source, so they're created mode 0700 under `$TMPDIR`. Override with
`SSA_WORK_DIR`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SSA_WORK_DIR` | `$TMPDIR/smart-subagents` | Task scratch root |
| `SSA_USAGE_PY` | alongside the script | Path to `ai-cli-usage.py` |
| `SSA_PREMIUM_MODELS` | `Fable,Opus` | Model-scoped weekly windows that gate whether your local session should do labor itself |
| `CODEX_BIN` / `GROK_BIN` / `KIMI_BIN` | auto-detected | Override worker binary paths |

`SSA_PREMIUM_MODELS` is the one worth setting. Claude's usage API reports weekly
caps scoped to individual models by display name. When your top-tier model is
near its cap, `local_labor_ok` goes false and the supervisor stops doing work
in-session even though cheaper models are still fine. Set this to whatever your
plan's premium tier is actually called.

## Known gaps

Task size gates quota thresholds but does not select a model tier. A one-line fix
and a forty-file port both get the flagship model on codex and grok; only kimi
drops to a fast model for `tiny` and `small` work. There's also no difficulty
axis distinct from size, and the two aren't the same thing: a fifteen-line
lock-free ring buffer is `small` and hard, a forty-file mechanical rename is
`large` and trivial. Cheap-model-for-easy-work routing isn't implemented yet.

The capability priors in `FIT` are hand-tuned constants, not measurements.

## License

MIT
