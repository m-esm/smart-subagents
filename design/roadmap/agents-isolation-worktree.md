---
state: shipped
lens: outside-in
created: 2026-09-11
metric: isolation keys written into claude ssa-worker agents markdown or --agents payload
before: 0 (measured 2026-09-11; _MARKDOWN_FM_ORDER, agents_markdown, agents_payload, and workers.json claude.agents all omit isolation)
target: 1
measure: python3 -c "import pathlib,re,json; ad=pathlib.Path('scripts/ssa/adapters.py').read_text(); w=json.loads(pathlib.Path('scripts/workers.json').read_text()); order=ad[ad.find('_MARKDOWN_FM_ORDER'):ad.find('def agents_markdown')]; md=ad[ad.find('def agents_markdown'):ad.find('def write_agents_file')]; pay=ad[ad.find('def agents_payload'):ad.find('def agents_json')]; print(int('isolation' in order or 'isolation' in md or 'isolation' in pay or 'isolation' in w['workers']['claude']['agents']))"
evidence:
  - design/roadmap/evidence/2026-09-11-agents-isolation-worktree.txt
slices: 2/2
after: 1
---
# Pin isolation: worktree on the Claude ssa-worker agent

## Why, against GOAL.md

GOAL.md done-means is isolated worktree labor that the parent does not write. Claude Code's official docs (https://code.claude.com/docs/en/worktrees and https://code.claude.com/docs/en/sub-agents, fetched 2026-09-11) say a custom subagent is isolated from the main checkout by adding `isolation: worktree` to its frontmatter; Bash/PowerShell then run inside that worktree and a command whose cwd resolves to the main checkout fails.

SSA already mints its own git worktree and pins `--agent ssa-worker` via `--agents` JSON plus `$WT/.claude/agents/ssa-worker.md`. The markdown renderer hardcodes `memory: project` and `background: true` and never writes `isolation`. `agents_payload` keys are only name/description/disallowedTools/prompt/(optional model). `workers.json` `claude.agents` keys are those four. Measure prints 0.

B1–B11 already shipped (agents JSON, limits, budget, resume, effort, fork, ssa-worker.md memory/maxTurns/background, model-force, docs/upstream-subagents.md, agent_teams). This field is the next official frontmatter key the renderer still drops.

Without `isolation: worktree` on the pinned agent, Claude's own enforcement (cwd must stay in the agent worktree) does not apply to nested Agent/Bash the way the docs describe. SSA's git worktree is necessary but not the same switch.

## What better looks like

`agents_markdown` emits `isolation: worktree`. `--agents` JSON still omits it: the session agent already runs inside SSA's git worktree, and JSON `isolation: worktree` would mint a second worktree off the default branch (https://code.claude.com/docs/en/sub-agents and https://x.com/masayaneg/status/2104864366700966308, 2026-10). Gate: the measure command prints 1. `tests/test_agents_file.py` asserts `fm["isolation"] == "worktree"` and `isolation` not in the JSON body.

## Slices

- [x] `agents_markdown` writes `isolation: worktree`; measure prints 1.
- [x] `test_agents_file.py` asserts the parsed frontmatter has `isolation == "worktree"`; `--agents` JSON still omits it, with a comment pointing at the docs.
