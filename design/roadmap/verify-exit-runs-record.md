---
state: promoted
lens: spec-gap
created: 2026-09-10
metric: cmd_record calls inside cmd_verify
before: 0 (measured 2026-09-10; cmd_verify never calls cmd_record)
target: 1
measure: python3 -c "import pathlib,re; t=pathlib.Path('scripts/smart-subagents.sh').read_text(); s=t.find('cmd_verify() {'); e=t.find(chr(10)+'cmd_', s+10); print(len(re.findall(r'\\bcmd_record\\b', t[s:e])))"
evidence:
  - design/roadmap/evidence/2026-09-10-cmd-record-in-verify.txt
  - design/roadmap/evidence/2026-09-10-verify-without-record.json
  - design/roadmap/evidence/2026-09-10-copilot-usage-records-auto.txt
slices: 0/3
after:
---
# Verify appends the outcome ledger row

## Why, against GOAL.md

GOAL.md's first number is dispatch verified-pass rate (`ledger --days 7`, target 90%). The same sentence claims "the outcome ledger `record` appends after each verify". The code does not. `cmd_verify` writes `outcome.json` and a `verified|failed|inconclusive` event; `cmd_dispatch` already calls it (shipped `dispatch-exit-runs-verify`). `cmd_record` is still a separate supervisor verb. The measure command prints 0 (`design/roadmap/evidence/2026-09-10-cmd-record-in-verify.txt`). This tick's workdir query: 23 finished tasks have `outcome.json`, 7 of them have no `outcome-record.json` (4 pass, 2 fail, 1 inconclusive) — `design/roadmap/evidence/2026-09-10-verify-without-record.json`. Those 7 never enter the ledger, so GOAL number 1 and number 3 (retry rate, same ledger) are a subset of runs a parent remembered to `record`. Comparable product: GitHub Copilot streams usage records when the session ends, without a second human command (`design/roadmap/evidence/2026-09-10-copilot-usage-records-auto.txt`).

## What better looks like

After verify writes `outcome.json`, it calls the existing `record` path with `--outcome` mapped from the verdict (`pass` → `verified-pass`, `fail` → `rejected`, `inconclusive` → `partial`). Retries stay 0 unless the task already has an attempt count; a steered run still forces 0. Supervisor `record --dir` remains for handoff notes and overrides. A second verify on the same dir does not append a duplicate row. Parent still reviews the diff.

## Slices

- [ ] `cmd_verify` calls `cmd_record`; the measure command prints 1.
- [ ] Verdict map: pass → verified-pass, fail → rejected, inconclusive → partial. A new finish with `outcome.json` also has `outcome-record.json`.
- [ ] Idempotent: re-verify of a dir that already has `outcome-record.json` does not double-append the ledger. Manual `record --dir` still accepts `--retries` / `--handoff-to` / `--notes`.
