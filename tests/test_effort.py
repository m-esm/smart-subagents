"""$DIR/effort.txt overrides derived worker-args effort, once, and is recorded.

Absent file: argv is byte-for-byte what dispatch already emitted. An unknown
rung fails dispatch before launch. The launched rung is `effort` on
outcome.json and the ledger record, never empty when a derived value existed.
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

CLAUDE_DERIVED = ["--effort", "medium", "--model", "fable"]
CLAUDE_AGENTS = (
    '{"ssa-worker":{"description":"SSA dispatched worker: Complete the fixture task.",'
    '"disallowedTools":["Task","Agent"],"model":"fable","prompt":"You are the dispatched '
    "worker and you are the labor. Complete the task in the brief yourself. Do not run "
    "smart-subagents.sh, do not spawn another CLI, do not delegate onward. Do not commit, "
    'do not push, do not reformat the tree."}}'
)


def claude_implement_argv(launch_brief, effort="medium"):
    return [
        "-p",
        "Read the file %s and complete the task it describes." % launch_brief,
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--effort",
        effort,
        "--model",
        "fable",
        "--agents",
        CLAUDE_AGENTS,
        "--agent",
        "ssa-worker",
    ]


def _claude_env(te):
    env = dict(te.env)
    env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
    env["SSA_ALLOW_UNSANDBOXED_WRITE"] = "1"
    return env


def _record(te, task_dir, env):
    rc, out, err = run_ssa(
        "record",
        "--dir",
        str(task_dir),
        "--outcome",
        "verified-pass",
        env=env,
    )
    return rc, out, err


class EffortRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_ssa("registry").load()

    def test_accepted_rungs_come_from_the_registry_not_a_hardcoded_list(self):
        claude = self.reg.get("claude")
        self.assertEqual(claude.effort_flags, ["--effort", "{effort}"])
        self.assertEqual(claude.effort_ladder, ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(self.reg.get("kimi").effort_flags, [])
        self.assertEqual(self.reg.get("kimi").effort_ladder, [])


class EffortArgvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapters = load_ssa("adapters")
        cls.reg = load_ssa("registry").load()

    def _build(self, tmp, worker, effort_text=None, effort_missing=False, args=None):
        brief = tmp / "brief.md"
        brief.write_text("Complete the fixture task.\n")
        ctx = {
            "worktree": str(tmp),
            "brief": str(brief),
            "args": list(args if args is not None else CLAUDE_DERIVED),
        }
        if effort_missing:
            ctx["effort_file"] = str(tmp / "no-such-effort.txt")
        elif effort_text is not None:
            path = tmp / "effort.txt"
            path.write_text(effort_text)
            ctx["effort_file"] = str(path)
        return self.adapters.build_command(worker, "implement", ctx, reg=self.reg)

    def test_absent_file_leaves_derived_args_untouched(self):
        with temp_env() as te:
            built = self._build(te.root, "claude")
            argv = built["argv"]
            self.assertEqual(argv.count("--effort"), 1)
            self.assertEqual(argv[argv.index("--effort") : argv.index("--effort") + 2],
                             ["--effort", "medium"])
            built = self._build(te.root, "claude", effort_missing=True)
            self.assertEqual(built["argv"].count("--effort"), 1)
            self.assertEqual(built["argv"][built["argv"].index("--effort") + 1], "medium")

    def test_override_replaces_derived_and_emits_one_pair(self):
        with temp_env() as te:
            built = self._build(te.root, "claude", "high\n")
            argv = built["argv"]
            self.assertEqual(argv.count("--effort"), 1)
            idx = argv.index("--effort")
            self.assertEqual(argv[idx : idx + 2], ["--effort", "high"])
            self.assertNotIn("medium", argv)

    def test_unknown_rung_raises_naming_the_file_value_and_ladder(self):
        with temp_env() as te:
            with self.assertRaises(self.adapters.AdapterError) as caught:
                self._build(te.root, "claude", "bogus\n")
            msg = str(caught.exception)
            self.assertIn("bogus", msg)
            self.assertIn("effort.txt", msg)
            for rung in self.reg.get("claude").effort_ladder:
                self.assertIn(rung, msg)

    def test_kimi_does_not_gain_an_effort_flag(self):
        with temp_env() as te:
            built = self._build(
                te.root, "kimi", "high\n", args=["-m", "kimi-for-coding-highspeed"]
            )
            self.assertNotIn("--effort", built["argv"])
            self.assertNotIn("high", built["argv"])
            self.assertIn("-m", built["argv"])

    def test_comments_only_is_not_an_override(self):
        with temp_env() as te:
            built = self._build(te.root, "claude", "# high\n\n  \n")
            self.assertEqual(built["argv"].count("--effort"), 1)
            self.assertEqual(built["argv"][built["argv"].index("--effort") + 1], "medium")


class EffortDispatchTests(unittest.TestCase):
    def test_absent_effort_txt_argv_matches_derived_exactly(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_DERIVED)
            env = _claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-claude" / "argv.txt")
            expected = claude_implement_argv(repo / "BRIEF.md", effort="medium")
            self.assertEqual(argv, expected)
            self.assertEqual(argv.count("--effort"), 1)

            outcome = json.loads((task_dir / "outcome.json").read_text())
            self.assertIn("effort", outcome)
            self.assertEqual(outcome["effort"], "medium")
            self.assertTrue(outcome["effort"])

            rec_rc, rec_out, rec_err = _record(te, task_dir, env)
            self.assertEqual(rec_rc, 0, rec_err)
            record = json.loads((task_dir / "outcome-record.json").read_text())
            self.assertIn("effort", record)
            self.assertEqual(record["effort"], "medium")
            self.assertTrue(record["effort"])

    def test_override_high_replaces_medium_and_emits_exactly_one_pair(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_DERIVED)
            (task_dir / "effort.txt").write_text("high\n")
            env = _claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-claude" / "argv.txt")
            expected = claude_implement_argv(repo / "BRIEF.md", effort="high")
            self.assertEqual(argv, expected)
            self.assertEqual(argv.count("--effort"), 1)
            idx = argv.index("--effort")
            self.assertEqual(argv[idx : idx + 2], ["--effort", "high"])
            self.assertNotIn("medium", argv)

            outcome = json.loads((task_dir / "outcome.json").read_text())
            self.assertEqual(outcome["effort"], "high")

            rec_rc, rec_out, rec_err = _record(te, task_dir, env)
            self.assertEqual(rec_rc, 0, rec_err)
            record = json.loads((task_dir / "outcome-record.json").read_text())
            self.assertEqual(record["effort"], "high")

    def test_recorded_effort_is_what_launched_not_what_the_file_says_later(self):
        """The record must be a fact about the run, not a re-read of the inputs.

        effort.txt is a task-dir file a supervisor may edit between the
        dispatch and the record (re-dispatch at a different rung, tidy-up,
        a second experiment). If `record` re-derives the value from the
        current files, the ledger says the run used a rung it never used,
        and every fit conclusion drawn from that column is wrong.
        """
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_DERIVED)
            (task_dir / "effort.txt").write_text("high\n")
            env = _claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-claude" / "argv.txt")
            idx = argv.index("--effort")
            self.assertEqual(argv[idx : idx + 2], ["--effort", "high"])

            # The run is over. Someone edits the input file.
            (task_dir / "effort.txt").write_text("low\n")

            rec_rc, rec_out, rec_err = _record(te, task_dir, env)
            self.assertEqual(rec_rc, 0, rec_err)
            record = json.loads((task_dir / "outcome-record.json").read_text())
            self.assertEqual(
                record["effort"],
                "high",
                "ledger recorded the edited file, not the launched rung",
            )
            outcome = json.loads((task_dir / "outcome.json").read_text())
            self.assertEqual(outcome["effort"], "high")

    def test_unknown_effort_dispatch_exits_nonzero_naming_file_value_and_ladder(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_DERIVED)
            (task_dir / "effort.txt").write_text("bogus\n")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=_claude_env(te),
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("bogus", err)
            self.assertIn("effort.txt", err)
            for rung in load_ssa("registry").load().get("claude").effort_ladder:
                self.assertIn(rung, err)
            self.assertFalse(
                (te.home / ".ssa-test" / "fake-claude" / "argv.txt").exists()
            )

    def test_kimi_with_effort_txt_does_not_inject_a_flag(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir, repo, worker_args=["-m", "kimi-for-coding-highspeed"]
            )
            (task_dir / "effort.txt").write_text("high\n")
            env = dict(te.env)
            env["KIMI_BIN"] = str(BIN_DIR / "fake-kimi")
            env["SSA_ALLOW_KIMI_WRITE"] = "1"
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "kimi", env=env
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-kimi" / "argv.txt")
            self.assertEqual(
                argv,
                [
                    "-p",
                    "Read the file %s and complete the task it describes."
                    % (repo / "BRIEF.md"),
                    "--output-format",
                    "stream-json",
                    "-m",
                    "kimi-for-coding-highspeed",
                ],
            )
            self.assertNotIn("--effort", argv)
            self.assertEqual(argv.count("--effort"), 0)

    def test_max_is_on_claude_ladder_and_dispatches(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_DERIVED)
            (task_dir / "effort.txt").write_text("max\n")
            env = _claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-claude" / "argv.txt")
            self.assertEqual(argv, claude_implement_argv(repo / "BRIEF.md", effort="max"))
            self.assertEqual(argv.count("--effort"), 1)
            outcome = json.loads((task_dir / "outcome.json").read_text())
            self.assertEqual(outcome["effort"], "max")


if __name__ == "__main__":
    unittest.main()
