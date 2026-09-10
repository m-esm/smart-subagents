# Upstream Claude Code subagents

Cited digest of Anthropic's current docs, fetched 2026-09-10.
Pages: `sub-agents`, `agent-sdk/subagents`, `changelog`, `workflows`, `agent-teams`.
Changelog HEAD on that fetch is **2.1.267** (9 September 2026).[3]

This is an upstream snapshot for SSA, not a product spec. SSA's Claude worker still runs `claude -p` from `scripts/workers.json`; implications that follow from `-p` are called out at the end.

## What a subagent is

A subagent is a separate agent instance that handles a focused subtask in its own context and returns a summary to the parent.[1][2] Use one when a side task would flood the main conversation with search results, logs, or file contents you will not need again.[1]

Benefits the docs name: context isolation, parallelization, specialized instructions, and tool restrictions.[2] Intermediate tool calls stay inside the subagent; only the final message returns.[2]

Subagents live inside one session. They are not agent view (background sessions you dispatch and monitor), not agent teams (experimental peer sessions with a shared task list), and not dynamic workflows (a JS script that orchestrates many agents).[1][4][5]

## Define them

Three ways:[2]

1. **Programmatic** (SDK `agents` / CLI `--agents` JSON). Recommended for SDK apps.[2]
2. **Filesystem** markdown in `.claude/agents/` (and `~/.claude/agents/`, plugins, managed settings).[1]
3. **Built-in `general-purpose`**, which Claude can spawn via the Agent tool with no definition of your own.[2]

Programmatic definitions win over filesystem files with the same name.[2]

### `--agents` JSON (CLI)

CLI-defined subagents exist only for that session.[1] One `--agents` value can define several. Keys are agent names; each object uses at least `description` and `prompt`, and may set `tools` and `model`:[1]

```bash
claude --agents '{
  "code-reviewer": {
    "description": "Expert code reviewer. Use proactively after code changes.",
    "prompt": "You are a senior code reviewer. Focus on code quality, security, and best practices.",
    "tools": ["Read", "Grep", "Glob", "Bash"],
    "model": "sonnet"
  }
}'
```

Scope priority, highest first: managed settings, `--agents`, `.claude/agents/`, `~/.claude/agents/`, plugin `agents/`.[1]

### Filesystem frontmatter

Required: `name`, `description`. Optional fields the current page lists: `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `skills`, `mcpServers`, `hooks`, `memory`, `background`, `effort`, `isolation`, `color`, `initialPrompt`, `experimental.cacheTtl`.[1]

The markdown body is the subagent system prompt. Subagents do not receive the Claude Code system prompt.[1]

As of **v2.1.198**, `/agents` no longer opens the creation wizard; ask Claude or edit `.claude/agents/` directly. Files, frontmatter, and locations are unchanged.[1][3]

### SDK `AgentDefinition`

Required: `description`, `prompt`. Optional: `tools`, `disallowedTools`, `model`, `skills`, `memory`, `mcpServers`, `initialPrompt`, `maxTurns`, `background`, `effort`, `permissionMode`.[2] Python keeps camelCase field names on the wire (`disallowedTools`, `mcpServers`).[2]

Include `Agent` in `allowedTools` / `allowed_tools` or Claude cannot spawn them.[2]

## Built-ins and invocation

Interactive sessions register built-ins by default: **Explore** (read-only search), **Plan** (plan-mode research), **general-purpose**, plus helpers (`claude`, `statusline-setup`, `claude-code-guide`).[1]

As of **v2.1.198**, Explore inherits the main session model (capped at Opus on the Claude API) instead of always using Haiku.[1][3]
`CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS=1` removes Explore and Plan (v2.1.198+).[1]
In `-p` / Agent SDK, `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` removes all built-ins; an Agent call without `subagent_type` then fails with `subagent_type is required`.[1][2]

Claude picks a subagent from `description`, or you name it in the prompt.[1][2]

## Foreground vs background

- Foreground blocks the parent until done; permission prompts pass through.[1]
- Background runs concurrently; permission prompts surface in the main session (auto-deny of those prompts ended before v2.1.186).[1]

Pick order for an Agent-tool spawn:[1]

1. In-process agent-team teammate spawn: always foreground (and `background: true` / `run_in_background: true` is refused in the cases the page lists).
2. `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`: always foreground.
3. Fork mode on (interactive default): always background; Claude cannot ask for foreground.
4. Fork mode off (`-p` and Agent SDK unless you enable it): background by default, foreground when Claude needs the result first. Frontmatter `background: true` forces background.[1]

**v2.1.198** made background-by-default the shipped behavior (it had been rolling out).[3] The SDK page: omitting `run_in_background` launches a background subagent; Claude sets `run_in_background: false` when it needs the result; `background: true` on the definition forces background regardless. Before v2.1.198 an omitted `run_in_background` could run synchronously.[2]

Background subagents get a smaller built-in tool set than foreground ones (forks and resumed foreground agents keep the original set).[1]

As of **v2.1.198**, a subagent treats messages from its launcher as normal task direction. No agent message counts as the user's permission approval, and no agent message can change permission settings, `CLAUDE.md`, or configuration.[1][3]

## Fork mode (v2.1.232)

A **fork** is a subagent that inherits the full conversation, system prompt, tools, model, and prompt cache instead of starting fresh. Only its own tool calls stay out of the parent context.[1]

Claude Code turns fork mode **on by default in interactive sessions** and **off by default in `-p` and the Agent SDK**. The interactive default requires **v2.1.232+**. Earlier versions needed `CLAUDE_CODE_FORK_SUBAGENT=1`.[1][3]

Changelog 2.1.232: `subagent_type: "fork"` inherits conversation and prompt cache; non-teammate spawns in interactive sessions run in the background by default.[3]

Override: `CLAUDE_CODE_FORK_SUBAGENT=1` also turns it on in `-p`/SDK; `=0` turns it off everywhere.[1] Deny `Agent(fork)` to keep background spawns without letting Claude request the fork type.[1]

`/subtask` starts a fork (v2.1.212+). A fork cannot spawn further forks.[1]

## Nesting, concurrency, spend

Default depth: **3** layers below the main conversation (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`). `1` turns nesting off.[1][2]

History: v2.1.172–2.1.216 nested up to 5 and the cap was not configurable; v2.1.217–2.1.218 defaulted to 1; v2.1.219 raised the default to 3.[1]

Default concurrency: **20** running Agent-tool subagents (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`, v2.1.217+). Ultracode sessions are exempt. Resumes and `/subtask` forks can exceed the cap.[1][2]

SDK spend: `maxBudgetUsd` / `max_budget_usd` counts subagent requests in `total_cost_usd` and can refuse further spawns, stop background subagents, and end the query with `error_max_budget_usd`. Depth/concurrency env vars in this form are documented for TypeScript SDK v0.3.219 and Python SDK v0.2.127 (Claude Code v2.1.219+).[2]

## Model and effort pins

Resolution order (v2.1.251+): per-invocation `model`, then definition `model` (`inherit` = main), then `CLAUDE_CODE_SUBAGENT_MODEL`, then the main model. Before v2.1.251 the env var overrode the other two.[1]

**v2.1.257**: `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` applies `CLAUDE_CODE_SUBAGENT_MODEL` (or the main model if that env is unset) to every subagent, teammate, and workflow agent, ignoring per-spawn and definition overrides. Forks and skills with `model: inherit` still follow the main model.[1][3]

**v2.1.198**: subagents inherit the session's extended thinking on/off. There is no per-subagent thinking setting. Before this, thinking was disabled inside subagents.[1][3]

**v2.1.267**: `effort:` frontmatter on custom commands, skills, and subagents was ignored on models whose default effort is still pinned (Opus 4.7, Opus 4.8, Fable 5); that is fixed. Subagents started with `--system-prompt` / `--append-system-prompt` now record the system prompt and tool definitions once for prompt-cache stability.[3]

## Dynamic workflows

A dynamic workflow is a JavaScript script that orchestrates many subagents at once. Claude writes it; a runtime executes it outside the conversation, so the parent context holds the final answer, not the loop.[4]

| | Subagents | Skills | Agent teams | Workflows |
| --- | --- | --- | --- | --- |
| What | Worker Claude spawns | Instructions Claude follows | Lead supervising peer sessions | Script the runtime executes |
| Who decides next | Claude, turn by turn | Claude, following the prompt | The lead, turn by turn | The script |
| Intermediate results | Context window | Context window | Shared task list | Script variables |
| Scale | A few delegated tasks per turn | Same | A handful of long-running peers | Dozens to hundreds per run |
| Interruption | Restarts the turn | Restarts the turn | Teammates keep running | Resumable in the same session |

Source: workflows comparison table.[4]

Use a workflow when the job outgrows a handful of subagents or you want findings cross-checked (codebase-wide audit, 500-file migration, multi-angle research).[4] Available on paid plans, Anthropic API, Bedrock, Google Cloud Agent Platform, and Microsoft Foundry; Pro enables them from `/config`.[4]

The SDK `Workflow` tool is in TypeScript Agent SDK v0.3.149+. Put `Workflow` in `allowedTools` to auto-approve runs.[2]

**v2.1.267** also fixed Workflow `agent()` calls with large output schemas being refused in auto mode instead of going through the safety classifier.[3]

## Agent teams (experimental)

Agent teams are experimental and **disabled by default**. Enable with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. Without it, no team is set up and Claude does not spawn or propose teammates.[5]

The teams page is current as of **v2.1.178**: with the flag set, spawning a teammate needs no setup step; `TeamCreate` / `TeamDelete` are gone; `team_name` on the Agent tool is accepted but ignored.[5]

| | Subagents | Agent teams |
| --- | --- | --- |
| Context | Own window; results return to caller | Own window; fully independent |
| Communication | Result to caller (named subagents can also message) | Teammates message each other |
| Coordination | Main agent manages all work | Self-coordination plus shared task list |
| Best for | Focused tasks where only the result matters | Work that needs discussion |
| Token cost | Lower (summaries) | Higher (each teammate is a full instance) |

Source: agent-teams comparison table.[5]

Enabling teams also changes ordinary delegation: a subagent Claude **names** from the main conversation launches as a teammate unless the call is a fork or passes `isolation`.[1][5]

**`-p` and Agent SDK never spawn teammates.** A named subagent stays an ordinary subagent even with the flag on.[5]

## Version pins (assigned)

| Version | Date (changelog) | Subagent-relevant change |
| --- | --- | --- |
| **2.1.198** | 1 July 2026 | Background-by-default shipped; Explore inherits main model (capped at opus); subagents inherit extended thinking; `/agents` wizard removed; launcher messages are task direction, never user approval; `claude agents` Notification hooks `agent_needs_input` / `agent_completed`.[3] |
| **2.1.232** | 13 August 2026 | Fork mode on by default in interactive sessions (`subagent_type: "fork"` inherits conversation + prompt cache); interactive non-teammate spawns background by default; completed subagent rows hide immediately with a `/tasks` footer hint.[3] |
| **2.1.257** | 1 September 2026 | `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`; Fable 5.1 default Fable model; subagents auto-continue after mid-stream cutoff instead of ending incomplete; live token counters for background subagents and teammates.[3] |
| **2.1.267** | 9 September 2026 | `effort:` frontmatter honored on pinned-effort models (incl. Fable 5); prompt-cache stability for subagents with `--system-prompt` / `--append-system-prompt`; Workflow `agent()` large-schema auto-mode classifier fix.[3] |

## Implications for this repo (`claude -p`)

SSA's Claude worker argv is print-mode (`-p`), not an interactive REPL (`scripts/workers.json`). From the pages above:

- Fork mode is **off** unless `CLAUDE_CODE_FORK_SUBAGENT=1`.[1]
- Background is still the default when Claude omits `run_in_background`; Claude sets it false when it needs the result before continuing.[1][2]
- Agent teams do not spawn teammates under `-p`.[5]
- `--agents '{"name":{"description","prompt","tools","model"}}'` is the session-scoped definition path that matches B1 (`dispatch --worker claude` building that JSON).[1]
- Built-ins can be stripped with `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1`.[1][2]
- Do not load the target repo's project settings: SSA already omits `--setting-sources project` so hooks in the worktree cannot run on the host.

## Sources

[1] https://code.claude.com/docs/en/sub-agents — Create custom subagents
[2] https://code.claude.com/docs/en/agent-sdk/subagents — Subagents in the SDK
[3] https://code.claude.com/docs/en/changelog — Claude Code changelog
[4] https://code.claude.com/docs/en/workflows — Orchestrate subagents at scale with dynamic workflows
[5] https://code.claude.com/docs/en/agent-teams — Orchestrate teams of Claude Code sessions
