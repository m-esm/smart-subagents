---
state: shipped
lens: outside-in
created: 2026-10-03
metric: permissionMode key written into claude agents_payload
before: 0 (measured 2026-10-03 on origin/main 83f969b; agents_payload omitted permissionMode)
target: 1
measure: python3 -c "import pathlib; ad=pathlib.Path('scripts/ssa/adapters.py').read_text(); s=ad[ad.find('def agents_payload'):ad.find('def agents_json')]; print(int('permissionMode' in s))"
evidence:
  - design/roadmap/evidence/2026-10-03-agents-json-permission-mode.txt
slices: 1/1
after: 1
---
# Pin permissionMode: acceptEdits on the session agent

## Why, against GOAL.md

https://code.claude.com/docs/en/sub-agents (fetched 2026-10-03) lists `permissionMode` on `--agents` JSON (`default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan`). SSA's argv already passes `--permission-mode acceptEdits` and never `bypassPermissions`. The named `ssa-worker` definition did not pin the same mode, so a client that honors the JSON over the CLI flag would drop acceptEdits. SSA hard-rule stays: never `bypassPermissions`.

## What better looks like

`agents_payload` sets `permissionMode: acceptEdits`. Tests assert that key on the JSON and that argv still contains no `bypassPermissions`. Gate: the measure command prints 1.

## Slices

- [x] `agents_payload` writes `permissionMode: acceptEdits`; measure prints 1.
