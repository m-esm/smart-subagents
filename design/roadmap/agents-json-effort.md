---
state: shipped
lens: outside-in
created: 2026-10-03
metric: effort key written into claude agents_payload
before: 0 (measured 2026-10-03 on origin/main 83f969b; agents_payload omitted effort)
target: 1
measure: python3 -c "import pathlib; ad=pathlib.Path('scripts/ssa/adapters.py').read_text(); s=ad[ad.find('def agents_payload'):ad.find('def agents_json')]; print(int('effort' in s))"
evidence:
  - design/roadmap/evidence/2026-10-03-agents-json-effort.txt
slices: 1/1
after: 1
---
# Copy the launched effort rung onto --agents JSON

## Why, against GOAL.md

Claude Code 2.1.267 made `effort:` on subagent definitions take effect (https://code.claude.com/docs/en/sub-agents, fetched 2026-10-03). SSA already maps difficulty to CLI `--effort` and records the rung on the ledger. The `--agents` body omitted `effort`, so a session agent that reads the definition instead of the CLI flag ran at the model default.

## What better looks like

`agents_payload` copies `launched_effort` onto `effort` when a rung exists, and omits the key when args have none. `tests/test_agents_file.py` asserts `effort == "high"` for `--effort high` and that empty args omit it. Gate: the measure command prints 1.

## Slices

- [x] `agents_payload` writes `effort` from the launched rung; measure prints 1.
