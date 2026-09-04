---
state: proposed
lens: outside-in
created: 2026-09-03
metric: top-level CLI verbs that inject a follow-up into a live dispatch
before: 0 (usage text 2026-09-04; no steer/followup/inject)
target: 1
measure: python3 -c "import subprocess,re; out=subprocess.check_output(['bash','scripts/smart-subagents.sh'], stderr=subprocess.STDOUT, text=True); print(len(re.findall(r'(?im)^  (steer|followup|follow-up|inject)\\b', out)))"
evidence:
  - design/roadmap/evidence/2026-09-04-copilot-mission-control.png
  - design/roadmap/evidence/2026-09-04-copilot-mission-control-extract.txt
slices: 0/3
after:
---
# Copilot-style mid-run steer (no extra dispatch)

## Why, against GOAL.md

GOAL.md's third number is briefs that needed a retry because the worker asked a question or stopped early (target under 10%). GitHub Copilot's comparable coding-agent product ships a Mission Control surface whose hero UI is a live session with Overview, Files changed, the session log, and a compose box that says **Steer this session while Copilot is working**. uiwalk of the 2025-10-28 changelog (2026-09-04) is `design/roadmap/evidence/2026-09-04-copilot-mission-control.png`. SSA's usage lists `dispatch --resume` and `stop`, and `state.py` has no needs-input edge; the measure command prints 0. A parent who wants to correct a live run must kill it or wait for exit and re-dispatch, which the ledger counts as a retry. Mid-run inject is what comparable products do so that correction is not a second attempt.

## What better looks like

Copilot: type into the live session; the agent adapts after the current tool call. SSA: `smart-subagents.sh steer --dir DIR --message TEXT` delivers one follow-up into the running worker (stdin or a follow-up file the wrapper already watches) without minting a new task or incrementing retries. `status` shows the session can accept steer. `dispatch --resume` stays the after-exit path.

## Slices

- [ ] `steer --dir DIR --message TEXT` refuses when the worker is not running, otherwise delivers the text and leaves the same task_id in `running`.
- [ ] `status --dir DIR` (and `ls`) show that a live session accepts steer; no new lifecycle state required beyond documenting the inject.
- [ ] Gate: the measure command prints 1. A steered run is the same dispatch in the ledger, not a retry.
