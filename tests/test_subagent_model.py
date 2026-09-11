"""CLAUDE_CODE_SUBAGENT_MODEL / _FORCE via limits.txt env_pass.

FORCE-without-MODEL still launches, writes model-downgraded.txt, and record
marks model_downgraded. The launched model is persisted in model-used.txt
and is what record reads. Log warnings that match the live 2.1.267 strings
also mark it. The assigned-but-absent phrase does not.
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
    run_ssa,
    temp_env,
)
from test_effort import CLAUDE_DERIVED  # noqa: E402
from test_shell import make_task_dir  # noqa: E402


def _claude_env(te):
    env = dict(te.env)
    env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
    env["SSA_ALLOW_UNSANDBOXED_WRITE"] = "1"
    env["SSA_TEST_LEAK"] = "1"
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = "parent-leak-model"
    env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] = "parent-leak-force"
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


def _recorded_env(te):
    return json.loads(
        (te.home / ".ssa-test" / "fake-claude" / "env.json").read_text()
    )


class SubagentModelDispatchTests(unittest.TestCase):
    def test_force_and_model_reach_the_scrubbed_process(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_DERIVED)
            (task_dir / "limits.txt").write_text(
                "subagent_model=haiku\nsubagent_model_force=1\n"
            )
            env = _claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertEqual(rc, 0, err)
            recorded = _recorded_env(te)
            self.assertEqual(recorded["CLAUDE_CODE_SUBAGENT_MODEL"], "haiku")
            self.assertEqual(recorded["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"], "1")
            self.assertNotIn("SSA_TEST_LEAK", recorded)
            self.assertNotEqual(
                recorded.get("CLAUDE_CODE_SUBAGENT_MODEL"), "parent-leak-model"
            )
            self.assertEqual(
                (task_dir / "model-used.txt").read_text().strip(), "haiku"
            )
            self.assertFalse((task_dir / "model-downgraded.txt").exists())
            outcome = json.loads((task_dir / "outcome.json").read_text())
            self.assertEqual(outcome["model"], "haiku")
            self.assertFalse(outcome.get("model_downgraded"))

            rec_rc, rec_out, rec_err = _record(te, task_dir, env)
            self.assertEqual(rec_rc, 0, rec_err)
            record = json.loads((task_dir / "outcome-record.json").read_text())
            self.assertEqual(record["model"], "haiku")
            self.assertFalse(record.get("model_downgraded"))

    def test_force_without_model_marks_model_downgraded_and_still_launches(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_DERIVED)
            (task_dir / "limits.txt").write_text("subagent_model_force=1\n")
            env = _claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertEqual(rc, 0, err)
            recorded = _recorded_env(te)
            self.assertEqual(recorded["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"], "1")
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", recorded)
            self.assertTrue((task_dir / "model-downgraded.txt").exists())
            self.assertEqual(
                (task_dir / "model-used.txt").read_text().strip(), "fable"
            )
            outcome = json.loads((task_dir / "outcome.json").read_text())
            self.assertTrue(outcome["model_downgraded"])
            self.assertEqual(outcome["model"], "fable")
            self.assertNotEqual(outcome.get("failure_class"), "rate-limit")
            self.assertNotEqual(outcome.get("failure_class"), "auth")
            self.assertFalse((task_dir / "cooldown.txt").exists())

            rec_rc, rec_out, rec_err = _record(te, task_dir, env)
            self.assertEqual(rec_rc, 0, rec_err)
            record = json.loads((task_dir / "outcome-record.json").read_text())
            self.assertEqual(record["model_downgraded"], True)
            self.assertEqual(record["model"], "fable")
            self.assertNotEqual(record.get("failure_class"), "rate-limit")
            self.assertNotEqual(record.get("failure_class"), "auth")

    def test_log_warning_marks_model_downgraded(self):
        cases = (
            (
                'Workflow agent model "opus" ignored: '
                "CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set\n",
                True,
            ),
            (
                'Model "opus" is restricted by your organization\'s settings. '
                "Using haiku instead.\n",
                True,
            ),
            ("requested subagent model is restricted\n", False),
        )
        for stderr, expect_mark in cases:
            with self.subTest(stderr=stderr, expect_mark=expect_mark):
                with temp_env() as te:
                    repo = make_git_repo(te.root / "repo")
                    task_dir = make_task_dir(
                        te.work_dir, repo, worker_args=CLAUDE_DERIVED
                    )
                    (te.home / ".ssa-test-control.json").write_text(
                        json.dumps({"exit_code": 0, "stderr": stderr})
                    )
                    env = _claude_env(te)
                    rc, out, err = run_ssa(
                        "dispatch",
                        "--dir",
                        str(task_dir),
                        "--worker",
                        "claude",
                        env=env,
                    )
                    self.assertEqual(rc, 0, err)
                    marked = (task_dir / "model-downgraded.txt").exists()
                    self.assertEqual(marked, expect_mark, stderr)

    def test_recorded_model_is_what_launched_not_what_limits_say_later(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_DERIVED)
            (task_dir / "limits.txt").write_text(
                "subagent_model=haiku\nsubagent_model_force=1\n"
            )
            env = _claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude", env=env
            )
            self.assertEqual(rc, 0, err)
            self.assertEqual(
                (task_dir / "model-used.txt").read_text().strip(), "haiku"
            )

            (task_dir / "limits.txt").write_text("subagent_model=opus\n")

            rec_rc, rec_out, rec_err = _record(te, task_dir, env)
            self.assertEqual(rec_rc, 0, rec_err)
            record = json.loads((task_dir / "outcome-record.json").read_text())
            self.assertEqual(
                record["model"],
                "haiku",
                "ledger recorded the edited limits.txt, not the launched model",
            )
            self.assertEqual(
                (task_dir / "model-used.txt").read_text().strip(), "haiku"
            )
            outcome = json.loads((task_dir / "outcome.json").read_text())
            self.assertEqual(outcome["model"], "haiku")


class SubagentModelUnitTests(unittest.TestCase):
    def test_absent_phrase_does_not_match_downgrade_re(self):
        adapters = load_ssa("adapters")
        self.assertIsNone(
            adapters.MODEL_DOWNGRADE_RE.search(
                "requested subagent model is restricted"
            )
        )
        self.assertIsNotNone(
            adapters.MODEL_DOWNGRADE_RE.search(
                'Workflow agent model "opus" ignored: '
                "CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set"
            )
        )
        self.assertIsNotNone(
            adapters.MODEL_DOWNGRADE_RE.search(
                'Model "opus" is restricted by your organization\'s settings. '
                "Using haiku instead."
            )
        )


if __name__ == "__main__":
    unittest.main()
