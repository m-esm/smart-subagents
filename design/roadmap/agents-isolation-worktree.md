---
state: building
lens: outside-in
created: 2026-09-11
metric: isolation keys written into claude ssa-worker agents markdown or --agents payload
before: 0 (measured 2026-09-11; _MARKDOWN_FM_ORDER, agents_markdown, agents_payload, and workers.json claude.agents all omit isolation)
target: 1
measure: python3 -c "import pathlib,re,json; ad=pathlib.Path('scripts/ssa/adapters.py').read_text(); w=json.loads(pathlib.Path('scripts/workers.json').read_text()); order=ad[ad.find('_MARKDOWN_FM_ORDER'):ad.find('def agents_markdown')]; md=ad[ad.find('def agents_markdown'):ad.find('def write_agents_file')]; pay=ad[ad.find('def agents_payload'):ad.find('def agents_json')]; print(int('isolation' in order or 'isolation' in md or 'isolation' in pay or 'isolation' in w['workers']['claude']['agents']))"
evidence:
  - design/roadmap/evidence/2026-09-11-agents-isolation-worktree.txt
slices: 1/2
after:
---
# Pin isolation: worktree on the Claude ssa-worker agent

## Why, against GOAL.md

GOAL.md done-means is isolated worktree labor that the parent does not write. Claude Code's official docs (https://code.claude.com/docs/en/worktrees and https://code.claude.com/docs/en/sub-agents, fetched 2026-09-11) say a custom subagent is isolated from the main checkout by adding `isolation: worktree` to its frontmatter; Bash/PowerShell then run inside that worktree and a command whose cwd resolves to the main checkout fails.

SSA already mints its own git worktree and pins `--agent ssa-worker` via `--agents` JSON plus `$WT/.claude/agents/ssa-worker.md`. The markdown renderer hardcodes `memory: project` and `background: true` and never writes `isolation`. `agents_payload` keys are only name/description/disallowedTools/prompt/(optional model). `workers.json` `claude.agents` keys are those four. Measure prints 0.

B1–B11 already shipped (agents JSON, limits, budget, resume, effort, fork, ssa-worker.md memory/maxTurns/background, model-force, docs/upstream-subagents.md, agent_teams). This field is the next official frontmatter key the renderer still drops.

Without `isolation: worktree` on the pinned agent, Claude's own enforcement (cwd must stay in the agent worktree) does not apply to nested Agent/Bash the way the docs describe. SSA's git worktree is necessary but not the same switch.

## What better looks like

`agents_markdown` emits `isolation: worktree` (file-only is fine, same as memory/background). Optional later: pass it through `--agents` JSON if Claude Code accepts it there (docs list `isolation` on both the markdown fields and the `--agents` JSON fields). Gate: the measure command prints 1. A fixture agent file parsed by `tests/test_agents_file.py` contains `isolation` equal to `worktree`.

## Slices

- [x] `agents_markdown` writes `isolation: worktree`; measure prints 1.
- [ ] `test_agents_file.py` asserts the parsed frontmatter has `isolation == "worktree"`; `--agents` JSON still omits it if that is the current Claude JSON contract, with a comment pointing at the docs.
