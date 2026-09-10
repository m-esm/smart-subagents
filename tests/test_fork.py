"""kind.txt=fork emits --fork-session once on a claude resume.

Fork is a modifier on resume, not a fourth argv mode. No resumable session
refuses before launch. The parent id is a fact of the run, snapshotted
before session-id.txt is overwritten from the child log.
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
from test_budget import CLAUDE_AGENTS, CLAUDE_WORKER_ARGS  # noqa: E402
from test_shell import make_task_dir  # noqa: E402

PARENT = "parent-session-aaaaaaaa"
CHILD = "child-session-bbbbbbbb"


def _claude_env(te):
    env = dict(te.env)
    env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
    env["SSA_ALLOW_UNSANDBOXED_WRITE"] = "1"
    return env


def claude_resume_argv(launch_brief, session_id, fork=False):
    argv = [
        "-p",
        "Read the file %s and complete the task it describes." % launch_brief,
        "--resume",
        session_id,
    ]
    if fork:
        argv.append("--fork-session")
    argv.extend(
        [
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--effort",
            "high",
            "--model",
            "fable",
            "--agents",
            CLAUDE_AGENTS,
            "--agent",
            "ssa-worker",
        ]
    )
    return argv


def _dispatch_claude(te, task_dir, **control):
    if control:
        (te.home / ".ssa-test-control.json").write_text(json.dumps(control))
    return run_ssa(
        "dispatch",
        "--dir",
        str(task_dir),
        "--worker",
        "claude",
        env=_claude_env(te),
    )


class ForkArgvTests(unittest.TestCase):
    def test_fork_with_session_emits_the_flag_once_on_the_resume_argv(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir, repo, kind="fork", worker_args=CLAUDE_WORKER_ARGS
            )
            (task_dir / "session-id.txt").write_text(PARENT + "\n")
            rc, out, err = _dispatch_claude(te, task_dir, session_id=CHILD)
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-claude" / "argv.txt")
            expected = claude_resume_argv(repo / "BRIEF.md", PARENT, fork=True)
            self.assertEqual(argv, expected)
            self.assertEqual(argv.count("--fork-session"), 1)

    def test_fork_without_session_refuses_nonzero_naming_both_files(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir, repo, kind="fork", worker_args=CLAUDE_WORKER_ARGS
            )
            rc, out, err = _dispatch_claude(te, task_dir)
            self.assertNotEqual(rc, 0)
            self.assertIn("kind.txt", err)
            self.assertIn("session-id.txt", err)
            self.assertFalse(
                (te.home / ".ssa-test" / "fake-claude" / "argv.txt").exists()
            )

        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir, repo, kind="fork", worker_args=CLAUDE_WORKER_ARGS
            )
            (task_dir / "session-id.txt").write_text(PARENT + "\n")
            (task_dir / "resume-unavailable.txt").write_text("none\n")
            rc, out, err = _dispatch_claude(te, task_dir)
            self.assertNotEqual(rc, 0)
            self.assertIn("kind.txt", err)
            self.assertIn("session-id.txt", err)
            self.assertFalse(
                (te.home / ".ssa-test" / "fake-claude" / "argv.txt").exists()
            )

    def test_parent_session_survives_the_child_id_overwrite(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir, repo, kind="fork", worker_args=CLAUDE_WORKER_ARGS
            )
            (task_dir / "session-id.txt").write_text(PARENT + "\n")
            rc, out, err = _dispatch_claude(te, task_dir, session_id=CHILD)
            self.assertEqual(rc, 0, err)
            self.assertEqual((task_dir / "session-id.txt").read_text().strip(), CHILD)
            self.assertTrue(
                (task_dir / "parent-session.txt").exists(),
                "parent-session.txt must exist as a fact of the run",
            )
            self.assertEqual(
                (task_dir / "parent-session.txt").read_text().strip(), PARENT
            )
            attempts = json.loads((task_dir / "task.json").read_text())["attempts"]
            self.assertEqual(attempts[-1]["parent_session"], PARENT)

            (task_dir / "session-id.txt").write_text("edited-after-the-run\n")
            self.assertEqual(
                (task_dir / "parent-session.txt").read_text().strip(), PARENT
            )

    def test_absent_or_other_kind_emits_no_fork_flag(self):
        cases = (
            ("absent", None),
            ("default", "default"),
            ("comments-only", "# fork\n\n  \n# still a comment\n"),
        )
        for label, kind_text in cases:
            with self.subTest(label=label):
                with temp_env() as te:
                    repo = make_git_repo(te.root / "repo")
                    task_dir = make_task_dir(
                        te.work_dir, repo, worker_args=CLAUDE_WORKER_ARGS
                    )
                    if kind_text is None:
                        (task_dir / "kind.txt").unlink()
                    else:
                        (task_dir / "kind.txt").write_text(kind_text)
                    rc, out, err = _dispatch_claude(te, task_dir)
                    self.assertEqual(rc, 0, err)
                    argv = read_argv_file(
                        te.home / ".ssa-test" / "fake-claude" / "argv.txt"
                    )
                    self.assertNotIn("--fork-session", argv)
                    self.assertEqual(argv.count("--fork-session"), 0)

        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                kind="fork",
                worker_args=["--reasoning-effort", "high"],
            )
            (task_dir / "session-id.txt").write_text(PARENT + "\n")
            env = dict(te.env)
            env["GROK_BIN"] = str(BIN_DIR / "fake-grok")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "grok", env=env
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-grok" / "argv.txt")
            self.assertNotIn("--fork-session", argv)
            self.assertEqual(argv.count("--fork-session"), 0)


class ForkRegistryTests(unittest.TestCase):
    def test_fork_flags_live_in_the_registry(self):
        reg = load_ssa("registry").load()
        self.assertEqual(reg.get("claude").fork_flags, ["--fork-session"])
        for name in ("codex", "grok", "kimi"):
            self.assertEqual(reg.get(name).fork_flags, [], name)


if __name__ == "__main__":
    unittest.main()
