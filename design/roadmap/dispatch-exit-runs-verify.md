---
state: shipped
lens: journey
created: 2026-09-06
metric: cmd_verify calls inside cmd_dispatch
before: 0 (measured 2026-09-06; cmd_dispatch body has no cmd_verify)
target: 1
measure: python3 -c "import pathlib,re; t=pathlib.Path('scripts/smart-subagents.sh').read_text(); s=t.find('cmd_dispatch() {'); e=t.find(chr(10)+'cmd_', s+10); print(len(re.findall(r'\\bcmd_verify\\b', t[s:e])))"
evidence:
  - design/roadmap/evidence/2026-09-06-copilot-coding-agent-self-review.png
  - design/roadmap/evidence/2026-09-06-copilot-coding-agent-self-review.txt
  - design/roadmap/evidence/2026-09-06-doctor-verify-pending.txt
  - design/roadmap/evidence/2026-09-06-finished-unverified.json
  - design/flows/brief-to-verified.json
slices: 3/3
after: 1
---
# Dispatch runs verify before it returns

## Why, against GOAL.md

GOAL.md's first number is dispatch verified-pass rate (`ledger --days 7`, target 90%). Done in GOAL.md is: hand it a repo and a brief, and init/dispatch "verifies the diff itself, and reports pass, fail or inconclusive" without trusting the worker's own claims. That end-to-end path is not completable today. `cmd_dispatch` never calls `cmd_verify` (measure prints 0). `doctor` on this host this tick flagged 6 finished tasks with `exit-code.txt` and no `outcome.json`, including grok task `1788523646-54165` with exit 0. The person who ran dispatch can only read `last-msg.txt`. Comparable product: GitHub Copilot coding agent reviews its own changes *before* it tags a human (uiwalk `design/roadmap/evidence/2026-09-06-copilot-coding-agent-self-review.png`; extract in the sibling `.txt`). Flow of the broken path: `design/flows/brief-to-verified.json`.

## What better looks like

After the worker process exits, dispatch runs the existing `verify --dir` path and writes `DIR/outcome.json` before it returns. Foreground exit code follows verify (0 pass, 1 fail, 2 inconclusive). Missing `verify-cmds.txt` is inconclusive, not a skip. Background runs do the same when they write `exit-code.txt`. `record` stays a supervisor step. Parent still reviews the diff.

## Slices

- [x] Foreground `dispatch --dir DIR`: after the worker exits, call `cmd_verify`; measure command prints 1.
- [x] Background / watchdog path writes `outcome.json` when it writes `exit-code.txt`; a new finish does not increment `doctor` `verify:pending`.
- [x] Missing `verify-cmds.txt` → inconclusive (`outcome.json` present, verdict inconclusive), never silent skip.
