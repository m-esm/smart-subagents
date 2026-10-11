#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import sys
from pathlib import Path

SSA_CMD = re.compile(r'(?:smart-subagents\.sh"?|"\$SSA"|\$SSA)\s+(\w[\w-]*)')
POLL_CMDS = {"status", "ls", "tail", "wait"}
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
PROMPT_KEY_CHARS = 400


def iter_records(path: Path):
    with open(path, errors="ignore") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def content_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                total += len(block.get("text", ""))
            elif kind == "tool_result":
                total += content_chars(block.get("content"))
            else:
                total += len(json.dumps(block))
        return total
    return 0


def first_user_text(record) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return (content or "")[:PROMPT_KEY_CHARS]


def parse_ts(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def recent(paths, cutoff: float):
    return [p for p in paths if p.stat().st_mtime >= cutoff]


def agent_types_from_parents(parents) -> dict:
    types = {}
    for parent in parents:
        for record in iter_records(parent):
            if record.get("type") != "assistant":
                continue
            for block in (record.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("name") != "Agent":
                    continue
                payload = block.get("input") or {}
                key = (payload.get("prompt") or "")[:PROMPT_KEY_CHARS]
                types[key] = payload.get("subagent_type", "general-purpose")
    return types


class RunProfile:
    def __init__(self, path: Path):
        self.path = path
        self.first_prompt = None
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.first_turn_tokens = None
        self.instruction_files = {}
        self.ssa_calls = collections.Counter()
        self.ssa_result_chars = collections.Counter()
        self.poll_turns = 0
        self.sleep_calls = 0
        self.background_dispatches = 0
        self.foreground_dispatches = 0
        self.started = None
        self.ended = None
        self._pending = {}

    def feed(self, record):
        stamp = record.get("timestamp")
        if stamp:
            self.started = self.started or stamp
            self.ended = stamp
        kind = record.get("type")
        if kind == "attachment":
            self._feed_attachment(record.get("attachment") or {})
        elif kind == "assistant":
            self._feed_assistant(record.get("message") or {})
        elif kind == "user":
            if self.first_prompt is None:
                self.first_prompt = first_user_text(record)
            self._feed_results(record.get("message") or {})

    def _feed_attachment(self, attachment):
        if attachment.get("type") != "instructions":
            return
        for entry in attachment.get("files", []):
            self.instruction_files[entry.get("path", "?")] = len(entry.get("content", ""))

    def _feed_assistant(self, message):
        usage = message.get("usage") or {}
        if usage:
            self.turns += 1
            total = sum(usage.get(k, 0) or 0 for k in USAGE_KEYS)
            self.input_tokens += total
            self.output_tokens += usage.get("output_tokens", 0) or 0
            if self.first_turn_tokens is None:
                self.first_turn_tokens = total
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            payload = block.get("input") or {}
            self._pending[block.get("id")] = (block.get("name"), payload)
            if block.get("name") != "Bash":
                continue
            command = payload.get("command", "")
            match = SSA_CMD.search(command)
            if match:
                sub = match.group(1)
                self.ssa_calls[sub] += 1
                if sub in POLL_CMDS:
                    self.poll_turns += 1
                if sub == "dispatch":
                    if "--background" in command or "--bg" in command:
                        self.background_dispatches += 1
                    else:
                        self.foreground_dispatches += 1
            if re.search(r"\bsleep\b", command):
                self.sleep_calls += 1

    def _feed_results(self, message):
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            pending = self._pending.get(block.get("tool_use_id"))
            if not pending or pending[0] != "Bash":
                continue
            match = SSA_CMD.search(pending[1].get("command", ""))
            if match:
                self.ssa_result_chars[match.group(1)] += content_chars(block.get("content"))

    @property
    def minutes(self) -> float:
        if not (self.started and self.ended):
            return 0.0
        return (parse_ts(self.ended) - parse_ts(self.started)).total_seconds() / 60


def profile(path: Path) -> RunProfile:
    run = RunProfile(path)
    for record in iter_records(path):
        run.feed(record)
    return run


def human(tokens: float) -> str:
    if tokens >= 1e6:
        return f"{tokens / 1e6:.1f}M"
    if tokens >= 1e3:
        return f"{tokens / 1e3:.0f}k"
    return f"{tokens:.0f}"


def audit(projects: Path, days: int, top: int, out=sys.stdout) -> dict:
    cutoff = datetime.datetime.now().timestamp() - days * 86400
    parents = recent(projects.glob("*/*.jsonl"), cutoff)
    agents = recent(projects.glob("*/*/subagents/agent-*.jsonl"), cutoff)
    types = agent_types_from_parents(parents)

    by_type = collections.defaultdict(lambda: {"runs": 0, "turns": 0, "input": 0, "output": 0})
    ssa_runs = []
    for path in agents:
        run = profile(path)
        kind = types.get(run.first_prompt or "", "?")
        bucket = by_type[kind]
        bucket["runs"] += 1
        bucket["turns"] += run.turns
        bucket["input"] += run.input_tokens
        bucket["output"] += run.output_tokens
        if kind == "smart-subagents":
            ssa_runs.append(run)
    ssa_runs.sort(key=lambda r: -r.input_tokens)

    print(f"subagent runs, last {days} days, under {projects}", file=out)
    print(f"{'type':<20} {'runs':>5} {'turns':>6} {'input':>8} {'output':>7} {'in/turn':>8} {'turns/run':>9}", file=out)
    for kind, b in sorted(by_type.items(), key=lambda kv: -kv[1]["input"]):
        per_turn = b["input"] / max(b["turns"], 1)
        per_run = b["turns"] / max(b["runs"], 1)
        print(f"{kind:<20} {b['runs']:>5} {b['turns']:>6} {human(b['input']):>8} {human(b['output']):>7} {human(per_turn):>8} {per_run:>9.0f}", file=out)

    if ssa_runs:
        prefixes = sorted(r.first_turn_tokens or 0 for r in ssa_runs)
        print(file=out)
        print(f"smart-subagents supervisor runs: {len(ssa_runs)}", file=out)
        print(
            f"first-turn prefix tokens: min={human(prefixes[0])} median={human(prefixes[len(prefixes) // 2])} max={human(prefixes[-1])}",
            file=out,
        )
        polls = sum(r.poll_turns for r in ssa_runs)
        sleeps = sum(r.sleep_calls for r in ssa_runs)
        bg = sum(r.background_dispatches for r in ssa_runs)
        fg = sum(r.foreground_dispatches for r in ssa_runs)
        print(f"poll turns={polls} sleep calls={sleeps} dispatches: background={bg} foreground={fg}", file=out)

        files = collections.defaultdict(lambda: [0, 0])
        for run in ssa_runs:
            for path, chars in run.instruction_files.items():
                files[path][0] += 1
                files[path][1] += chars
        print("instruction files injected per turn (avg chars, runs):", file=out)
        for path, (count, chars) in sorted(files.items(), key=lambda kv: -kv[1][1]):
            print(f"  {chars // count / 1000:>6.0f}k x{count:<3} {path}", file=out)

        calls = collections.Counter()
        chars = collections.Counter()
        for run in ssa_runs:
            calls.update(run.ssa_calls)
            chars.update(run.ssa_result_chars)
        print("ssa subcommand results (calls, total chars, avg chars):", file=out)
        for sub, n in calls.most_common():
            print(f"  {sub:<16} n={n:<5} total={human(chars[sub]):>7} avg={chars[sub] // max(n, 1)}", file=out)

        print(file=out)
        print(f"{'input':>8} {'turns':>5} {'min':>6} {'polls':>5} file", file=out)
        for run in ssa_runs[:top]:
            label = f"{run.path.parts[-4][:30]}/{run.path.name[6:14]}"
            print(f"{human(run.input_tokens):>8} {run.turns:>5} {run.minutes:>6.0f} {run.poll_turns:>5} {label}", file=out)

    return {"by_type": dict(by_type), "ssa_runs": len(ssa_runs)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Token audit of Claude Code subagent transcripts, with a smart-subagents supervisor breakdown."
    )
    parser.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"), help="Claude Code projects dir")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--top", type=int, default=20, help="supervisor runs to list")
    args = parser.parse_args(argv)
    projects = Path(args.projects)
    if not projects.is_dir():
        print(f"not a directory: {projects}", file=sys.stderr)
        return 2
    audit(projects, args.days, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
