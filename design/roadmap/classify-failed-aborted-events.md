---
state: proposed
lens: telemetry
created: 2026-09-07
metric: _ssa_event aborted/verify-verdict lines that pass --failure-class
before: 0 (measured 2026-09-07; aborted and $verdict_state events omit it)
target: 2
measure: python3 -c "import pathlib,re; t=pathlib.Path('scripts/smart-subagents.sh').read_text().replace(chr(92)+chr(10),' '); print(sum(1 for m in re.finditer(r'_ssa_event[^\n]+', t) if (('--phase aborted' in m.group(0) or 'verdict_state' in m.group(0)) and '--failure-class' in m.group(0))))"
evidence:
  - design/roadmap/evidence/2026-09-07-events-failure-class-null.json
  - design/roadmap/evidence/2026-09-07-ledger-7d-and-doctor.txt
slices: 0/3
after:
---
# Failed and aborted events must carry failure_class

## Why, against GOAL.md

GOAL.md's first number is dispatch verified-pass rate (`ledger --days 7`, target 90%). This tick that command shows 100 dispatches and 76% overall (codex 75%, grok 88%, empty-worker 0%). The 24 non-passes are why the number is not 90%, but the product's own event log cannot say which class they were. Query of live `events.jsonl` this tick: 25 failed/aborted/stalled rows, `failure_class` null on 25/25 (`design/roadmap/evidence/2026-09-07-events-failure-class-null.json`). Worker exit already runs `_classify_failure` and can pass `--failure-class` on `exited`/`stalled`. `cmd_verify` writes phase `$verdict_state` with no class, and `stop` writes phase `aborted` with no class. 19 of 60 task-id dirs also have `429` in logs, which never lands on those failed/aborted rows. Without a class on the event, pick() and the ledger cannot tell a 429 from a verify fail from a supervisor stop, so the 90% number has no actionable denominator.

## What better looks like

Every failed, aborted, and stalled event carries a non-empty `failure_class` from a small enum (`rate-limited`, `verify-fail`, `stopped`, `stalled`, `env-blocked`, `unknown`). Stop/abort uses `stopped`. Verify uses the verdict (`verify-fail` / `inconclusive`). Worker-exit keeps `_classify_failure`. New failed/aborted rows in `events.jsonl` are not null.

## Slices

- [ ] `_ssa_event` / `_ssa_state` for `--phase aborted` pass `--failure-class` (stop → `stopped`).
- [ ] Verify's `$verdict_state` event passes `--failure-class` derived from the verdict (`failed` → `verify-fail`).
- [ ] Gate: the measure command prints 2. A newly finished failed or aborted task has non-null `failure_class` on that event.
