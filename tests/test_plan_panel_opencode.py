import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import FIXTURES_DIR, SSA_CLI_PY, make_git_repo, run_ssa, temp_env  # noqa: E402
from test_shell import write_script, write_usage_stub  # noqa: E402

REJECTED = FIXTURES_DIR / "providers" / "cerebras" / "opencode-plan-brief-rejected.jsonl"

OPENCODE_GATE = """#!{python}
import json, os, re, sys
argv = sys.argv[1:]
root = os.path.realpath(argv[argv.index("--dir") + 1])
brief = os.path.realpath(re.search(r"Read the file (\\S+) and", argv[-1]).group(1))
with open({record!r}, "a") as fh:
    fh.write(json.dumps({{"root": root, "brief": brief, "argv": argv}}) + "\\n")
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({{"type": "step_start", "part": {{"type": "step-start"}}}})
if os.path.commonpath([root, brief]) != root:
    sys.stderr.write("permission requested: external_directory (%s/*); auto-rejecting\\n"
                     % os.path.dirname(brief))
    emit({{"type": "tool_use", "part": {{"type": "tool", "tool": "read", "state": {{
        "status": "error", "input": {{"filePath": brief}},
        "error": "The user rejected permission to use this specific tool call."}}}}}})
    emit({{"type": "step_finish", "part": {{"type": "step-finish"}}}})
    sys.exit(0)
body = open(brief).read()
emit({{"type": "tool_use", "part": {{"type": "tool", "tool": "read", "state": {{
    "status": "completed", "input": {{"filePath": brief}}, "output": "ok"}}}}}})
lens = re.search(r"## Your lens\\n\\n(.+)", body).group(1)
emit({{"type": "text", "part": {{"type": "text", "text": "## Approach\\n\\n" + lens}}}})
"""

CLI_WITHOUT_VERDICT = """#!{python}
import runpy, sys
if sys.argv[1:2] == ["plan-verdict"]:
    sys.exit(1)
sys.argv[0] = {cli!r}
runpy.run_path({cli!r}, run_name="__main__")
"""


class OpencodePlanPanelTests(unittest.TestCase):
    def _env(self, te, worker_bin):
        stub = te.root / "usage-stub.py"
        write_usage_stub(
            stub,
            {
                "primary_worker": "deepseek",
                "fallback_workers": [],
                "ranked": [{"cli": "deepseek", "score": 90}],
                "worker_args": {"deepseek": ["--variant", "high"]},
                "reasons": [],
            },
        )
        env = dict(te.env)
        env["SSA_USAGE_PY"] = str(stub)
        env["SSA_STUB_ARGV"] = str(te.root / "usage-argv.txt")
        env["DEEPSEEK_WORKER_BIN"] = str(worker_bin)
        return env

    def _gate(self, te):
        record = te.root / "opencode-calls.jsonl"
        body = OPENCODE_GATE.format(python=sys.executable, record=str(record))
        return write_script(te.root / "fake-opencode-deepseek", body), record

    def test_every_opencode_planner_reads_its_brief_inside_the_worktree(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            fake, record = self._gate(te)
            rc, out, err = run_ssa(
                "plan", "--repo", str(repo), "--n", "3", "--goal", "Plan it.",
                env=self._env(te, fake),
            )
            self.assertEqual(rc, 0, err)
            doc = json.loads(out)
            self.assertEqual(doc["usable_plans"], 3, json.dumps(doc, indent=2))
            for plan in doc["plans"]:
                self.assertFalse(plan["empty"], plan)
                text = Path(plan["file"]).read_text()
                self.assertTrue(text.startswith("## Approach"), text)
                self.assertNotIn('"type"', text)
            calls = [json.loads(l) for l in record.read_text().splitlines()]
            self.assertEqual(len(calls), 3)
            self.assertEqual(len({c["brief"] for c in calls}), 3)
            for call in calls:
                self.assertTrue(call["brief"].startswith(call["root"] + "/"), call)
            wt = Path(doc["worktree"])
            self.assertFalse((wt / ".ssa").exists())
            status = subprocess.check_output(
                ["git", "-C", str(wt), "status", "--porcelain", "--ignored"], text=True
            )
            self.assertEqual(status, "")
            self.assertNotIn("dirty", doc)
            brief = (Path(doc["dir"]) / "brief-0-pragmatic.md").read_text()
            self.assertIn("Read the repository at %s." % calls[0]["root"], brief)

    def test_a_planner_refused_its_brief_is_reported_empty_with_the_reason(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            fake = write_script(
                te.root / "refusing-opencode",
                "#!/bin/sh\n/bin/cat %s\n" % REJECTED,
            )
            rc, out, err = run_ssa(
                "plan", "--repo", str(repo), "--n", "2", "--goal", "Plan it.",
                env=self._env(te, fake),
            )
            self.assertEqual(rc, 0, err)
            doc = json.loads(out)
            self.assertEqual(doc["usable_plans"], 0)
            for plan in doc["plans"]:
                self.assertTrue(plan["empty"], plan)
                self.assertIn("first tool call failed (read)", plan["reason"])
                self.assertIn("rejected permission", plan["reason"])
                text = Path(plan["file"]).read_text()
                self.assertTrue(text.startswith("(planner produced no plan:"), text)
            log = Path(doc["dir"]) / "plan-0.log"
            self.assertIn('"tool_use"', log.read_text())

    def test_an_event_only_stub_is_empty_when_the_verdict_step_fails(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            fake = write_script(
                te.root / "refusing-opencode",
                "#!/bin/sh\n/bin/cat %s\n" % REJECTED,
            )
            cli = write_script(
                te.root / "cli-without-verdict.py",
                CLI_WITHOUT_VERDICT.format(python=sys.executable, cli=str(SSA_CLI_PY)),
            )
            env = self._env(te, fake)
            env["SSA_CLI_PY"] = str(cli)
            rc, out, err = run_ssa(
                "plan", "--repo", str(repo), "--n", "1", "--goal", "Plan it.", env=env,
            )
            self.assertEqual(rc, 0, err)
            doc = json.loads(out)
            self.assertEqual(doc["usable_plans"], 0, json.dumps(doc, indent=2))
            self.assertTrue(doc["plans"][0]["empty"])
            self.assertEqual(doc["plans"][0]["reason"], "no plan text, only event-stream lines")
            self.assertFalse((Path(doc["dir"]) / "plan-0.verdict.json").exists())


if __name__ == "__main__":
    unittest.main()
