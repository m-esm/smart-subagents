import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ROOT  # noqa: E402

AUDIT_PY = ROOT / "scripts" / "ssa-token-audit.py"


def load_audit():
    spec = importlib.util.spec_from_file_location("ssa_token_audit", AUDIT_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def usage(cache_read=0, cache_create=0, out=5):
    return {
        "input_tokens": 3,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
        "output_tokens": out,
    }


def assistant(blocks, use, stamp):
    return {"type": "assistant", "timestamp": stamp, "message": {"usage": use, "content": blocks}}


def user_text(text, stamp):
    return {"type": "user", "timestamp": stamp, "message": {"content": [{"type": "text", "text": text}]}}


def tool_result(tool_use_id, text, stamp):
    return {
        "type": "user",
        "timestamp": stamp,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]},
    }


def bash(tool_use_id, command):
    return {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": command}}


def write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


SSA_PROMPT = "Dispatch one implementation task to an external worker and verify it. Repo: /tmp/x"
GP_PROMPT = "Design a bracket. Read-only."


class TokenAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        parent = self.projects / "-Users-x-repo" / "sess-1.jsonl"
        write_jsonl(
            parent,
            [
                assistant(
                    [
                        {"type": "tool_use", "id": "t1", "name": "Agent", "input": {"subagent_type": "smart-subagents", "prompt": SSA_PROMPT}},
                        {"type": "tool_use", "id": "t2", "name": "Agent", "input": {"subagent_type": "general-purpose", "prompt": GP_PROMPT}},
                    ],
                    usage(cache_read=100000),
                    "2026-10-01T10:00:00Z",
                )
            ],
        )
        ssa_agent = self.projects / "-Users-x-repo" / "sess-1" / "subagents" / "agent-aaa111.jsonl"
        write_jsonl(
            ssa_agent,
            [
                user_text(SSA_PROMPT, "2026-10-01T10:00:01Z"),
                {
                    "type": "attachment",
                    "timestamp": "2026-10-01T10:00:01Z",
                    "attachment": {"type": "instructions", "files": [{"path": "/Users/x/.claude/CLAUDE.md", "content": "x" * 4000}]},
                },
                assistant([bash("b1", 'bash "$SSA" dispatch --dir /d --background')], usage(cache_create=80000), "2026-10-01T10:00:02Z"),
                tool_result("b1", "smart-subagents: worker=grok", "2026-10-01T10:00:03Z"),
                assistant([bash("b2", "sleep 30; bash \"$SSA\" status --dir /d")], usage(cache_read=80000, cache_create=500), "2026-10-01T10:00:40Z"),
                tool_result("b2", "task : x\nstate : running\n", "2026-10-01T10:01:10Z"),
                assistant([bash("b3", 'bash "$SSA" status --dir /d')], usage(cache_read=80500, cache_create=200), "2026-10-01T10:05:00Z"),
                tool_result("b3", "state : verified\n", "2026-10-01T10:05:01Z"),
            ],
        )
        gp_agent = self.projects / "-Users-x-repo" / "sess-1" / "subagents" / "agent-bbb222.jsonl"
        write_jsonl(
            gp_agent,
            [
                user_text(GP_PROMPT, "2026-10-01T11:00:00Z"),
                assistant([], usage(cache_create=90000), "2026-10-01T11:00:01Z"),
                assistant([], usage(cache_read=90000), "2026-10-01T11:00:05Z"),
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_classifies_runs_by_parent_subagent_type(self):
        audit = load_audit()
        out = io.StringIO()
        summary = audit.audit(self.projects, days=36500, top=5, out=out)
        self.assertEqual(summary["ssa_runs"], 1)
        by_type = summary["by_type"]
        self.assertEqual(by_type["smart-subagents"]["turns"], 3)
        self.assertEqual(by_type["smart-subagents"]["input"], 3 * 3 + 80000 + 80500 + 80700)
        self.assertEqual(by_type["general-purpose"]["runs"], 1)
        self.assertEqual(by_type["general-purpose"]["input"], 3 * 2 + 90000 + 90000)

    def test_profile_counts_polls_dispatches_and_prefix(self):
        audit = load_audit()
        run = audit.profile(self.projects / "-Users-x-repo" / "sess-1" / "subagents" / "agent-aaa111.jsonl")
        self.assertEqual(run.first_turn_tokens, 80003)
        self.assertEqual(run.background_dispatches, 1)
        self.assertEqual(run.foreground_dispatches, 0)
        self.assertEqual(run.poll_turns, 2)
        self.assertEqual(run.sleep_calls, 1)
        self.assertEqual(run.instruction_files, {"/Users/x/.claude/CLAUDE.md": 4000})
        self.assertEqual(run.ssa_result_chars["status"], len("task : x\nstate : running\n") + len("state : verified\n"))
        self.assertAlmostEqual(run.minutes, 5.0, places=1)

    def test_report_names_prefix_and_instruction_files(self):
        audit = load_audit()
        out = io.StringIO()
        audit.audit(self.projects, days=36500, top=5, out=out)
        text = out.getvalue()
        self.assertIn("smart-subagents supervisor runs: 1", text)
        self.assertIn("first-turn prefix tokens: min=80k median=80k max=80k", text)
        self.assertIn("/Users/x/.claude/CLAUDE.md", text)
        self.assertIn("poll turns=2 sleep calls=1 dispatches: background=1 foreground=0", text)

    def test_main_rejects_missing_projects_dir(self):
        audit = load_audit()
        self.assertEqual(audit.main(["--projects", str(self.projects / "nope")]), 2)


if __name__ == "__main__":
    unittest.main()
