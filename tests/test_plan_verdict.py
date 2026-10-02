import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import FIXTURES_DIR, ROOT, load_ssa  # noqa: E402

digest = load_ssa("digest")
registry = load_ssa("registry")

OPENCODE = FIXTURES_DIR / "providers" / "cerebras"
REJECTED = (OPENCODE / "opencode-plan-brief-rejected.jsonl").read_text()
SUCCESS = (OPENCODE / "opencode-run.jsonl").read_text()


def text_event(text):
    return json.dumps({"type": "text", "part": {"type": "text", "text": text}})


def tool_event(tool, status, error=""):
    state = {"status": status, "input": {"filePath": "/tmp/fixture/x.md"}}
    if error:
        state["error"] = error
    return json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": tool, "state": state}})


class PlannerFailureTests(unittest.TestCase):
    def setUp(self):
        self.spec = registry.load(cache=False).get("deepseek")

    def failure(self, text, fmt="jsonl"):
        s = self.spec
        return digest.planner_failure_from_text(text, fmt, s.final, s.error, s.tool, s.tool_error)

    def test_a_planner_refused_its_brief_is_a_failed_planner(self):
        reason = self.failure(REJECTED)
        self.assertTrue(reason.startswith("first tool call failed (read):"), reason)
        self.assertIn("rejected permission", reason)

    def test_preamble_text_does_not_hide_a_rejected_first_call(self):
        lines = REJECTED.splitlines()
        stream = "\n".join([lines[0], text_event("Reading the brief first."), *lines[1:]])
        self.assertTrue(self.failure(stream).startswith("first tool call failed (read):"))

    def test_narration_then_a_failed_last_call_is_a_failed_planner(self):
        stream = "\n".join([
            tool_event("read", "completed"),
            text_event("Now checking the shell entry point."),
            tool_event("read", "error", "File not found"),
        ])
        reason = self.failure(stream)
        self.assertTrue(reason.startswith("run ended on a failed tool call (read):"), reason)

    def test_a_failed_call_followed_by_the_plan_is_a_plan(self):
        stream = "\n".join([
            tool_event("read", "completed"),
            tool_event("read", "error", "File not found"),
            text_event("## Approach\n\nShip it."),
        ])
        self.assertEqual(self.failure(stream), "")

    def test_a_real_successful_opencode_run_is_a_plan(self):
        self.assertEqual(self.failure(SUCCESS), "")

    def test_no_assistant_text_is_a_failed_planner(self):
        stream = tool_event("read", "completed")
        self.assertEqual(self.failure(stream), "no final assistant message")

    def test_a_text_planner_that_printed_only_events_has_no_plan(self):
        self.assertEqual(self.failure(REJECTED, fmt="text"), "no plan text, only event-stream lines")

    def test_a_text_planner_is_judged_on_its_text(self):
        self.assertEqual(self.failure("## Plan\n\n1. Do it.\n", fmt="text"), "")
        self.assertEqual(self.failure("   \n", fmt="text"), "no final assistant message")


class RegistryToolRuleTests(unittest.TestCase):
    def test_both_opencode_workers_declare_tool_rules(self):
        reg = registry.load(cache=False)
        for name in ("cerebras", "deepseek"):
            spec = reg.get(name)
            self.assertEqual(spec.tool["match"], {"type": "tool_use"})
            self.assertEqual(spec.tool_error["match"]["part.state.status"], "error")

    def test_a_tool_rule_without_tool_error_does_not_load(self):
        doc = json.loads((ROOT / "scripts" / "workers.json").read_text())
        del doc["workers"]["cerebras"]["tool_error"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workers.json"
            path.write_text(json.dumps(doc))
            with self.assertRaises(registry.RegistryError):
                registry.load(str(path), cache=False)


class PlannerVerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.plan = self.dir / "plan-0-architecture-deepseek.md"
        self.log = self.dir / "plan-0.log"
        self.log.write_text("opencode-deepseek: stderr line\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stdout_jsonl_planner_keeps_the_plan_and_moves_events_to_the_log(self):
        self.plan.write_text("\n".join([
            tool_event("read", "completed"),
            text_event("## Approach\n\nShip it."),
        ]) + "\n")
        self.assertEqual(digest.planner_verdict("deepseek", str(self.plan), str(self.log)),
                         {"empty": False})
        self.assertEqual(self.plan.read_text(), "## Approach\n\nShip it.\n")
        log = self.log.read_text()
        self.assertTrue(log.startswith("opencode-deepseek: stderr line\n"))
        self.assertIn('"type": "tool_use"', log)

    def test_a_refused_planner_is_empty_with_its_reason_and_a_digest(self):
        self.plan.write_text(REJECTED)
        verdict = digest.planner_verdict("deepseek", str(self.plan), str(self.log))
        self.assertTrue(verdict["empty"])
        self.assertIn("rejected permission", verdict["reason"])
        text = self.plan.read_text()
        self.assertTrue(text.startswith(digest.NO_PLAN_MARK))
        self.assertNotIn('"type": "step_start"', text)
        self.assertIn("log:", text)

    def test_a_text_planner_file_is_left_alone(self):
        self.plan = self.dir / "plan-0-risk-grok.md"
        self.plan.write_text("## Plan\n\nKeep it.\n")
        self.assertEqual(digest.planner_verdict("grok", str(self.plan), str(self.log)),
                         {"empty": False})
        self.assertEqual(self.plan.read_text(), "## Plan\n\nKeep it.\n")

    def test_a_text_planner_file_of_events_only_is_empty(self):
        self.plan = self.dir / "plan-0-risk-grok.md"
        self.plan.write_text(REJECTED)
        verdict = digest.planner_verdict("grok", str(self.plan), str(self.log))
        self.assertEqual(verdict, {"empty": True, "reason": "no plan text, only event-stream lines"})
        self.assertTrue(self.plan.read_text().startswith(digest.NO_PLAN_MARK))

    def test_an_unavailable_binary_marker_stays_empty(self):
        self.plan.write_text("(planner produced no output; deepseek binary unavailable)\n")
        verdict = digest.planner_verdict("deepseek", str(self.plan), str(self.log))
        self.assertEqual(verdict, {"empty": True, "reason": "deepseek binary unavailable"})


if __name__ == "__main__":
    unittest.main()
