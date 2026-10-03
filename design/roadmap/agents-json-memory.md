---
state: shipped
lens: outside-in
created: 2026-10-03
metric: memory key written into claude agents_payload
before: 0 (measured 2026-10-03 on origin/main 83f969b; agents_payload omitted memory)
target: 1
measure: python3 -c "import pathlib; ad=pathlib.Path('scripts/ssa/adapters.py').read_text(); s=ad[ad.find('def agents_payload'):ad.find('def agents_json')]; print(int('\"memory\"' in s))"
evidence:
  - design/roadmap/evidence/2026-10-03-agents-json-memory.txt
slices: 1/1
after: 1
---
# Put memory: project on --agents JSON

## Why, against GOAL.md

Current Claude Code docs (https://code.claude.com/docs/en/sub-agents, fetched 2026-10-03) list `memory` on both markdown frontmatter and `--agents` JSON. Scope priority puts `--agents` above `.claude/agents/`. SSA already wrote `memory: project` on the markdown file and omitted it from JSON, so the session agent launched with `--agent ssa-worker` never got project memory. That is a research note pretending to be a practice.

## What better looks like

`agents_payload` includes `memory: project`. `--agents` JSON and the markdown file both carry it. `tests/test_agents_file.py` asserts `json_body["memory"] == "project"`. Gate: the measure command prints 1.

## Slices

- [x] `agents_payload` writes `memory: project`; measure prints 1.
