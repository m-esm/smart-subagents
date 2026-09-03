# GOAL (draft by Pawl 2026-09-03, edit freely)

For a developer paying for several AI coding CLIs (Claude Code, Codex, Grok, Kimi) who wants routine coding labor delegated automatically instead of burning premium quota or babysitting rate limits by hand. Done means: hand it a repo and a brief, and `smart-subagents.sh init` / `dispatch` picks a worker with live headroom and the right capability fit, does the work in an isolated git worktree, verifies the diff itself, and reports pass, fail or inconclusive, without the parent session writing to the user's checkout or trusting the worker's own claims. It is explicitly NOT a general multi-agent framework, not a replacement for the parent's review of the final diff, and never labor in the supervisor session.

## Numbers that prove it
- dispatch verified-pass rate: `bash scripts/smart-subagents.sh ledger --days 7` (the outcome ledger `record` appends after each verify) - today: unknown, no outcomes recorded on this checkout; target: 90% verified pass
- test suite: `bash tests/run.sh` (smoke + unittest, 221 test functions) - today: smoke 22/22 observed, full run not timed; target: 221/221 in under 2 minutes
- briefs that needed a retry because the worker asked a question or stopped early, from the same ledger - today: unknown; target: under 10%

source: README.md, agents/smart-subagents.md, CHANGELOG.md, scripts/smart-subagents.sh, tests/run.sh
