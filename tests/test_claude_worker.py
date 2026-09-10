"""Shipped Claude/Fable worker: registry contract, argv, write gate.

Fable is not a fourth binary. It is `claude -p --model fable`. These tests
pin that the shipped registry says so, that a dispatch never opts into
bypassPermissions, and that a write run is refused until the unsandboxed
override is set (same capability gate as kimi, not a kimi-named one).
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (  # noqa: E402
    BIN_DIR,
    load_ssa,
    load_usage_module,
    make_git_repo,
    read_argv_file,
    run_ssa,
    run_ssa_cli,
    temp_env,
)
from test_shell import make_task_dir  # noqa: E402


class ShippedClaudeRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_ssa("registry").load()

    def test_claude_is_a_shipped_worker(self):
        self.assertIn("claude", self.reg.names)
        spec = self.reg.get("claude")
        self.assertEqual(spec.probe, "check_claude")
        self.assertEqual(spec.sandbox, "none")
        self.assertFalse(spec.write_allowed_default)
        self.assertEqual(spec.cwd_mode, "worktree")
        # Scrubbed: env_keep is HOME, PATH, TMPDIR, TERM, USER. USER is
        # required for the macOS keychain lookup; without it claude -p
        # reports "Not logged in".
        self.assertTrue(spec.env_scrub)
        self.assertEqual(
            spec.env_keep, ["HOME", "PATH", "TMPDIR", "TERM", "USER"]
        )
        self.assertEqual(spec.binary_env, "CLAUDE_BIN")

    def test_every_difficulty_pins_model_fable(self):
        spec = self.reg.get("claude")
        for difficulty in ("trivial", "routine", "hard", "frontier"):
            for size in ("tiny", "small", "medium", "large"):
                self.assertEqual(
                    spec.model_for(difficulty, size),
                    "fable",
                    msg="%s/%s" % (difficulty, size),
                )

    def test_implement_argv_is_print_mode_accept_edits_never_bypass(self):
        spec = self.reg.get("claude")
        argv = spec.argv_for("implement")
        self.assertIn("-p", argv)
        self.assertIn("{prompt}", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("acceptEdits", argv)
        joined = " ".join(argv)
        self.assertNotIn("dangerously-skip-permissions", joined)
        self.assertNotIn("bypassPermissions", joined)
        self.assertEqual(spec.prompt_for("implement")["transport"], "file-ref")

    def test_plan_argv_uses_plan_permission_mode(self):
        argv = self.reg.get("claude").argv_for("plan")
        self.assertIn("--permission-mode", argv)
        self.assertIn("plan", argv)
        self.assertNotIn("acceptEdits", argv)

    def test_effort_ladder_includes_xhigh(self):
        spec = self.reg.get("claude")
        self.assertEqual(spec.effort_ladder, ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(spec.effort_flags, ["--effort", "{effort}"])

    def test_workers_list_prints_claude_as_no_write(self):
        rc, out, err = run_ssa_cli("workers")
        self.assertEqual(rc, 0, err)
        rows = [line.split("\t") for line in out.strip().splitlines()]
        claude = next((row for row in rows if row[0] == "claude"), None)
        self.assertIsNotNone(claude, out)
        self.assertEqual(claude[2], "none")
        self.assertEqual(claude[3], "no-write")
        self.assertEqual(claude[4], "check_claude")


class ClaudeWorkerArgsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_worker_args_carry_effort_and_fable(self):
        self.assertEqual(
            self.m.worker_args("claude", "hard", "medium"),
            ["--effort", "high", "--model", "fable"],
        )
        self.assertEqual(
            self.m.worker_args("claude", "frontier", "large"),
            ["--effort", "xhigh", "--model", "fable"],
        )
        self.assertEqual(
            self.m.worker_args("claude", "trivial", "tiny"),
            ["--effort", "low", "--model", "fable"],
        )

    def test_recommend_can_pick_claude_when_it_is_the_only_survivor(self):
        rec = self.m.recommend(
            [
                self.m.CliStatus(
                    cli="claude",
                    available=True,
                    eligible=True,
                    score=80.0,
                    effective_score=80.0,
                    admission_score=80.0,
                )
            ],
            task_size="medium",
            difficulty="routine",
        )
        self.assertEqual(rec["primary_worker"], "claude")

    def test_cli_flag_choices_list_claude_once(self):
        choices = self.m.cli_check_names()
        self.assertEqual(choices.count("claude"), 1)
        self.assertEqual(choices[0], "claude")
        self.assertEqual(choices[-1], "all")
        self.assertIn("codex", choices)


class ClaudeDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_ssa("registry").load()

    def test_write_dispatch_is_refused_without_unsandboxed_override(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=["--effort", "high", "--model", "fable"])
            env = dict(te.env)
            env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
            env.pop("SSA_ALLOW_UNSANDBOXED_WRITE", None)
            env.pop("SSA_ALLOW_KIMI_WRITE", None)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("no sandbox", err)
            self.assertIn("SSA_ALLOW_UNSANDBOXED_WRITE=1", err)
            self.assertFalse((task_dir / "write-override.txt").exists())

    def test_write_dispatch_runs_with_unsandboxed_override_and_exact_argv(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                worker_args=["--effort", "high", "--model", "fable"],
            )
            env = dict(te.env)
            env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
            env["SSA_ALLOW_UNSANDBOXED_WRITE"] = "1"
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertEqual(rc, 0, err)
            recorder = te.home / ".ssa-test" / "fake-claude"
            argv = read_argv_file(recorder / "argv.txt")
            supervisor_brief = task_dir / "brief.md"
            launch_brief = repo / "BRIEF.md"
            self.assertTrue(supervisor_brief.is_file())
            # The staged copy is a launch path only: dispatch removes it when
            # the run ends, so it can never reach a diff or a `git add -A`.
            self.assertFalse(launch_brief.exists())
            porcelain = subprocess.check_output(
                ["git", "-C", str(repo), "status", "--porcelain", "-uall"],
                text=True,
            )
            self.assertNotIn("BRIEF.md", porcelain)
            self.assertEqual(
                argv,
                [
                    "-p",
                    "Read the file %s and complete the task it describes." % launch_brief,
                    "--output-format",
                    "json",
                    "--permission-mode",
                    "acceptEdits",
                    "--effort",
                    "high",
                    "--model",
                    "fable",
                    "--agents",
                    '{"ssa-worker":{"description":"SSA dispatched worker: Complete the fixture task.","disallowedTools":["Task","Agent"],"model":"fable","prompt":"You are the dispatched worker and you are the labor. Complete the task in the brief yourself. Do not run smart-subagents.sh, do not spawn another CLI, do not delegate onward. Do not commit, do not push, do not reformat the tree."}}',
                    "--agent",
                    "ssa-worker",
                ],
            )
            # --setting-sources project would load the TARGET repo's
            # .claude/settings.json and run its hooks unsandboxed.
            self.assertNotIn("--setting-sources", argv)
            self.assertNotIn("--dangerously-skip-permissions", argv)
            self.assertNotIn("bypassPermissions", " ".join(argv))
            self.assertNotIn("--add-dir", argv)
            recorded_cwd = (recorder / "cwd.txt").read_text().strip()
            self.assertEqual(os.path.realpath(recorded_cwd), os.path.realpath(str(repo)))
            self.assertTrue(
                os.path.realpath(str(launch_brief)).startswith(
                    os.path.realpath(str(repo)) + os.sep
                )
            )
            self.assertTrue((task_dir / "write-override.txt").exists())
            self.assertEqual((task_dir / "exit-code.txt").read_text().strip(), "0")
            self.assertTrue((task_dir / "session-id.txt").read_text().strip())

    def _build_claude(self, mode, brief_text, args, tmp, session_id=""):
        adapters = load_ssa("adapters")
        brief = tmp / "brief.md"
        brief.write_text(brief_text)
        ctx = {
            "worktree": str(tmp),
            "brief": str(brief),
            "args": args,
        }
        if session_id:
            ctx["session_id"] = session_id
        return adapters.build_command("claude", mode, ctx, reg=self.reg)

    def test_agents_json_model_comes_from_worker_args_and_is_omitted_when_absent(self):
        with temp_env() as te:
            built = self._build_claude(
                "implement",
                "Complete the fixture task.\n",
                ["--effort", "high", "--model", "sonnet"],
                te.root,
            )
            payload = json.loads(built["argv"][built["argv"].index("--agents") + 1])
            self.assertEqual(payload["ssa-worker"]["model"], "sonnet")
            self.assertNotEqual(payload["ssa-worker"]["model"], "fable")

            built = self._build_claude(
                "implement",
                "Complete the fixture task.\n",
                ["--effort", "high"],
                te.root,
            )
            payload = json.loads(built["argv"][built["argv"].index("--agents") + 1])
            self.assertNotIn("model", payload["ssa-worker"])

    def test_agents_description_uses_first_heading_else_first_line_and_truncates(self):
        with temp_env() as te:
            built = self._build_claude(
                "implement",
                "# Fix the widget\n\nBody of the brief.\n",
                ["--model", "sonnet"],
                te.root,
            )
            payload = json.loads(built["argv"][built["argv"].index("--agents") + 1])
            self.assertEqual(
                payload["ssa-worker"]["description"],
                "SSA dispatched worker: Fix the widget",
            )

            built = self._build_claude(
                "implement",
                "Just do it\n\n# Later heading is ignored\n",
                ["--model", "sonnet"],
                te.root,
            )
            payload = json.loads(built["argv"][built["argv"].index("--agents") + 1])
            self.assertEqual(
                payload["ssa-worker"]["description"],
                "SSA dispatched worker: Just do it",
            )

            heading = "#" + "W" * 400
            built = self._build_claude(
                "implement",
                heading + "\n\nbody\n",
                ["--model", "sonnet"],
                te.root,
            )
            payload = json.loads(built["argv"][built["argv"].index("--agents") + 1])
            self.assertEqual(
                payload["ssa-worker"]["description"],
                "SSA dispatched worker: " + ("W" * 200),
            )
            self.assertEqual(len(payload["ssa-worker"]["description"]), 23 + 200)

    def test_agents_json_disallows_task_and_agent(self):
        with temp_env() as te:
            built = self._build_claude(
                "implement",
                "Complete the fixture task.\n",
                [],
                te.root,
            )
            payload = json.loads(built["argv"][built["argv"].index("--agents") + 1])
            self.assertEqual(
                payload["ssa-worker"]["disallowedTools"],
                ["Task", "Agent"],
            )

    def test_plan_mode_pins_ssa_worker_and_keeps_plan_permission(self):
        spec = self.reg.get("claude")
        self.assertEqual(spec.agents["name"], "ssa-worker")
        for mode in ("implement", "plan", "resume"):
            argv = spec.argv_for(mode)
            self.assertIn("{agents}", argv, mode)
            self.assertIn("--agent", argv, mode)
            self.assertEqual(argv[argv.index("--agent") + 1], "{agent}", mode)
        argv = spec.argv_for("plan")
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertNotIn("acceptEdits", argv)

        with temp_env() as te:
            built = self._build_claude(
                "plan",
                "Complete the fixture task.\n",
                ["--effort", "high"],
                te.root,
            )
            argv = built["argv"]
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
            self.assertEqual(argv[argv.index("--agent") + 1], "ssa-worker")
            self.assertIn("--agents", argv)
            self.assertNotIn("acceptEdits", argv)

    def test_legacy_kimi_write_env_still_unlocks_claude(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            env = dict(te.env)
            env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
            env["SSA_ALLOW_KIMI_WRITE"] = "1"
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertEqual(rc, 0, err)
            self.assertTrue((task_dir / "write-override.txt").exists())


if __name__ == "__main__":
    unittest.main()
