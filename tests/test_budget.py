"""$DIR/budget.txt -> --max-budget-usd, and a budget halt is its own class.

A task ceiling is not an account problem: classify it as budget-exhausted,
never rate-limit, and do not bench the worker.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (  # noqa: E402
    BIN_DIR,
    load_ssa,
    make_git_repo,
    read_argv_file,
    run_ssa,
    temp_env,
)
from test_shell import make_task_dir  # noqa: E402

CLAUDE_WORKER_ARGS = ["--effort", "high", "--model", "fable"]
CLAUDE_AGENTS = (
    '{"ssa-worker":{"description":"SSA dispatched worker: Complete the fixture task.",'
    '"disallowedTools":["Task","Agent"],"model":"fable","prompt":"You are the dispatched '
    "worker and you are the labor. Complete the task in the brief yourself. Do not run "
    "smart-subagents.sh, do not spawn another CLI, do not delegate onward. Do not commit, "
    'do not push, do not reformat the tree."}}'
)
BUDGET_HALT = {
    "type": "result",
    "subtype": "error_max_budget_usd",
    "is_error": True,
    "result": None,
    "terminal_reason": "budget_exhausted",
    "session_id": "fake-claude-session-000000000001",
    "total_cost_usd": 0.437786,
}


def claude_implement_argv(launch_brief, budget=None):
    argv = [
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
    ]
    if budget is not None:
        argv.extend(["--max-budget-usd", budget])
    argv.extend(["--agents", CLAUDE_AGENTS, "--agent", "ssa-worker"])
    return argv


def _claude_env(te):
    env = dict(te.env)
    env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
    env["SSA_ALLOW_UNSANDBOXED_WRITE"] = "1"
    return env


class BudgetRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_ssa("registry").load()

    def test_claude_declares_budget_and_other_workers_do_not(self):
        spec = self.reg.get("claude")
        self.assertEqual(spec.budget_flags, ["--max-budget-usd", "{budget}"])
        for mode in ("implement", "plan", "resume"):
            self.assertIn("{budget}", spec.argv_for(mode), mode)
        for name in ("codex", "grok", "kimi"):
            other = self.reg.get(name)
            self.assertEqual(other.budget_flags, [], name)
            for mode, tokens in other.argv.items():
                self.assertNotIn("{budget}", tokens, (name, mode))


class BudgetArgvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapters = load_ssa("adapters")
        cls.reg = load_ssa("registry").load()

    def _build(self, tmp, worker, budget_text=None, budget_missing=False, args=None):
        brief = tmp / "brief.md"
        brief.write_text("Complete the fixture task.\n")
        ctx = {
            "worktree": str(tmp),
            "brief": str(brief),
            "args": list(args if args is not None else CLAUDE_WORKER_ARGS),
        }
        if budget_missing:
            ctx["budget"] = str(tmp / "no-such-budget.txt")
        elif budget_text is not None:
            path = tmp / "budget.txt"
            path.write_text(budget_text)
            ctx["budget"] = str(path)
        return self.adapters.build_command(worker, "implement", ctx, reg=self.reg)

    def test_budget_value_splices_two_tokens_after_model(self):
        with temp_env() as te:
            built = self._build(te.root, "claude", "2.50\n")
            argv = built["argv"]
            idx = argv.index("--max-budget-usd")
            self.assertEqual(argv[idx : idx + 2], ["--max-budget-usd", "2.50"])
            self.assertEqual(argv[argv.index("--model") + 1], "fable")
            self.assertEqual(argv[argv.index("--model") + 2], "--max-budget-usd")

    def test_comments_and_blanks_are_not_a_directive(self):
        with temp_env() as te:
            built = self._build(te.root, "claude", "# 9.99\n\n  \n# 1.00\n")
            self.assertNotIn("--max-budget-usd", built["argv"])

    def test_absent_budget_file_adds_no_flag(self):
        with temp_env() as te:
            built = self._build(te.root, "claude")
            self.assertNotIn("--max-budget-usd", built["argv"])
            built = self._build(te.root, "claude", budget_missing=True)
            self.assertNotIn("--max-budget-usd", built["argv"])

    def test_malformed_values_raise_naming_the_file(self):
        for raw in ("abc", "0", "-1", ""):
            with self.subTest(raw=repr(raw)):
                with temp_env() as te:
                    with self.assertRaises(self.adapters.AdapterError) as caught:
                        self._build(te.root, "claude", raw if raw == "" else raw + "\n")
                    msg = str(caught.exception)
                    self.assertIn("budget.txt", msg)

    def test_grok_ignores_budget_txt(self):
        with temp_env() as te:
            built = self._build(te.root, "grok", "2.50\n", args=["--reasoning-effort", "high"])
            self.assertNotIn("--max-budget-usd", built["argv"])
            self.assertNotIn("2.50", built["argv"])


class BudgetDispatchTests(unittest.TestCase):
    def test_budget_txt_2_50_is_adjacent_max_budget_usd(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_WORKER_ARGS)
            (task_dir / "budget.txt").write_text("2.50\n")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=_claude_env(te),
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-claude" / "argv.txt")
            launch_brief = repo / "BRIEF.md"
            self.assertEqual(argv, claude_implement_argv(launch_brief, budget="2.50"))
            idx = argv.index("--max-budget-usd")
            self.assertEqual(argv[idx : idx + 2], ["--max-budget-usd", "2.50"])

    def test_no_budget_txt_argv_matches_b1_exactly(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_WORKER_ARGS)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=_claude_env(te),
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-claude" / "argv.txt")
            launch_brief = repo / "BRIEF.md"
            self.assertEqual(argv, claude_implement_argv(launch_brief))
            self.assertNotIn("--max-budget-usd", argv)
            self.assertNotIn("", argv)

    def test_malformed_budget_dispatch_exits_nonzero_naming_the_file(self):
        for raw in ("abc", "0", "-1", ""):
            with self.subTest(raw=repr(raw)):
                with temp_env() as te:
                    repo = make_git_repo(te.root / "repo")
                    task_dir = make_task_dir(
                        te.work_dir, repo, worker_args=CLAUDE_WORKER_ARGS
                    )
                    (task_dir / "budget.txt").write_text(
                        raw if raw == "" else raw + "\n"
                    )
                    rc, out, err = run_ssa(
                        "dispatch", "--dir", str(task_dir), "--worker", "claude",
                        env=_claude_env(te),
                    )
                    self.assertNotEqual(rc, 0)
                    self.assertIn("budget.txt", err)
                    self.assertFalse(
                        (te.home / ".ssa-test" / "fake-claude" / "argv.txt").exists()
                    )

    def test_grok_with_budget_txt_gets_no_extra_argv(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                worker_args=["--reasoning-effort", "high"],
            )
            (task_dir / "budget.txt").write_text("2.50\n")
            env = dict(te.env)
            env["GROK_BIN"] = str(BIN_DIR / "fake-grok")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "grok", env=env
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-grok" / "argv.txt")
            self.assertNotIn("--max-budget-usd", argv)
            self.assertNotIn("2.50", argv)
            self.assertEqual(
                argv,
                [
                    "-p",
                    "Read the file %s and complete the task it describes."
                    % (repo / "BRIEF.md"),
                    "--cwd",
                    str(repo),
                    "--sandbox",
                    "workspace",
                    "--reasoning-effort",
                    "high",
                    "--output-format",
                    "streaming-messages-json",
                ],
            )


class BudgetHaltTests(unittest.TestCase):
    def test_budget_halt_classifies_as_itself_and_does_not_bench(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_WORKER_ARGS)
            (te.home / ".ssa-test-control.json").write_text(
                json.dumps({"exit_code": 1, "output": BUDGET_HALT})
            )
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=_claude_env(te),
            )
            self.assertEqual((task_dir / "exit-code.txt").read_text().strip(), "1")
            log = (task_dir / "stdout.log").read_text()
            adapters = load_ssa("adapters")
            msgs = adapters.error_strings(log)
            self.assertTrue(msgs, log)
            self.assertTrue(any("budget" in m.lower() for m in msgs), msgs)
            self.assertEqual(adapters.classify_log(1, str(task_dir / "stdout.log")), "budget-exhausted")
            self.assertNotEqual(
                adapters.classify_log(1, str(task_dir / "stdout.log")), "rate-limit"
            )
            attempts = json.loads((task_dir / "task.json").read_text())["attempts"]
            self.assertEqual(attempts[-1]["failure_class"], "budget-exhausted")
            outcome = json.loads((task_dir / "outcome.json").read_text())
            self.assertEqual(outcome["failure_class"], "budget-exhausted")
            self.assertFalse((task_dir / "cooldown.txt").exists())
            cooldown_path = te.state_dir / "cooldowns.json"
            if cooldown_path.exists():
                data = json.loads(cooldown_path.read_text())
                self.assertNotIn("claude", data)
            rec_rc, rec_out, rec_err = run_ssa(
                "record",
                "--dir", str(task_dir),
                "--outcome", "partial",
                env=_claude_env(te),
            )
            self.assertEqual(rec_rc, 0, rec_err)
            record = json.loads((task_dir / "outcome-record.json").read_text())
            self.assertEqual(record["failure_class"], "budget-exhausted")
            self.assertNotEqual(record["failure_class"], "rate-limit")

    def test_a_log_naming_both_budget_and_a_rate_limit_is_budget(self):
        """Ordering in classify_failure is load-bearing, not decorative.

        A budget halt can land in the same log as an earlier 429 notice.
        Whichever pattern is checked first decides, and calling that run
        rate-limit benches a healthy worker for every other task.
        """
        adapters = load_ssa("adapters")
        both = "usage limit reached earlier; error_max_budget_usd budget_exhausted"
        self.assertEqual(adapters.classify_failure(1, both), "budget-exhausted")
        self.assertEqual(
            adapters.classify_failure(1, "http 429 then budget_exhausted"),
            "budget-exhausted",
        )
        # A real rate limit with no budget wording still benches.
        self.assertEqual(
            adapters.classify_failure(1, "usage limit reached"), "rate-limit"
        )


if __name__ == "__main__":
    unittest.main()
