"""Dead-session claude --resume is session-missing, not a mystery crash.

A resume against an id the CLI does not have prints a bare stderr line, exits
1, and used to classify as unknown. That lets a supervisor retry --resume
into the same wall. Classify it, mark the task unresumable, do not bench.
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

DEAD_SID = "00000000-0000-0000-0000-000000000000"
DEAD_STDERR = "No conversation found with session ID: %s\n" % DEAD_SID
LIVE_SID = "fake-claude-session-000000000001"


def _claude_env(te):
    env = dict(te.env)
    env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
    env["SSA_ALLOW_UNSANDBOXED_WRITE"] = "1"
    return env


def _write_control(home, **payload):
    (home / ".ssa-test-control.json").write_text(json.dumps(payload))


def claude_resume_argv(launch_brief, session_id):
    return [
        "-p",
        "Read the file %s and complete the task it describes." % launch_brief,
        "--resume",
        session_id,
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


def _dispatch_resume(te, session_id, control):
    repo = make_git_repo(te.root / "repo")
    task_dir = make_task_dir(te.work_dir, repo, worker_args=CLAUDE_WORKER_ARGS)
    (task_dir / "session-id.txt").write_text(session_id + "\n")
    _write_control(te.home, **control)
    rc, out, err = run_ssa(
        "dispatch",
        "--dir",
        str(task_dir),
        "--worker",
        "claude",
        "--resume",
        env=_claude_env(te),
    )
    return repo, task_dir, rc, out, err


class DeadSessionResumeTests(unittest.TestCase):
    def test_dead_session_resume_is_session_missing_not_unknown(self):
        with temp_env() as te:
            repo, task_dir, rc, out, err = _dispatch_resume(
                te, DEAD_SID, {"exit_code": 1, "stderr": DEAD_STDERR}
            )
            self.assertEqual((task_dir / "exit-code.txt").read_text().strip(), "1")
            log_path = task_dir / "stdout.log"
            log = log_path.read_text()
            self.assertIn("No conversation found with session ID", log)
            adapters = load_ssa("adapters")
            self.assertEqual(
                list(adapters._iter_json_lines(log)),
                [],
                "empty-stdout resume must take the non-JSON tail path: %r" % log,
            )
            self.assertEqual(adapters.classify_log(1, str(log_path)), "session-missing")
            self.assertNotEqual(
                adapters.classify_log(1, str(log_path)), "unknown"
            )
            attempts = json.loads((task_dir / "task.json").read_text())["attempts"]
            self.assertEqual(attempts[-1]["failure_class"], "session-missing")
            self.assertNotEqual(attempts[-1]["failure_class"], "unknown")
            self.assertTrue((task_dir / "resume-unavailable.txt").exists())

    def test_session_missing_marks_unresumable_even_when_an_id_was_parsed(self):
        """The stale-id path must do work the empty-sid path was not already doing.

        A dead resume normally prints nothing on stdout, so session-id.txt is
        empty and the pre-existing `if [[ -z "$sid" ]]` branch writes
        resume-unavailable.txt on its own. That makes the happy assertion pass
        with the session-missing branch deleted entirely. Here the worker emits
        a parseable envelope WITH a session id and still fails with the stale-id
        wording, so only the session-missing branch can mark the dir unresumable.
        """
        with temp_env() as te:
            repo, task_dir, rc, out, err = _dispatch_resume(
                te,
                DEAD_SID,
                {
                    "exit_code": 1,
                    "stderr": DEAD_STDERR,
                    "output": {
                        "type": "result",
                        "subtype": "success",
                        "session_id": LIVE_SID,
                    },
                },
            )
            # The id parsed, so the empty-sid branch cannot be what fires.
            self.assertEqual(
                (task_dir / "session-id.txt").read_text().strip(), LIVE_SID
            )
            attempts = json.loads((task_dir / "task.json").read_text())["attempts"]
            self.assertEqual(attempts[-1]["failure_class"], "session-missing")
            self.assertTrue(
                (task_dir / "resume-unavailable.txt").exists(),
                "session-missing must mark the dir unresumable on its own",
            )

    def test_session_missing_does_not_bench_the_worker(self):
        with temp_env() as te:
            repo, task_dir, rc, out, err = _dispatch_resume(
                te, DEAD_SID, {"exit_code": 1, "stderr": DEAD_STDERR}
            )
            self.assertEqual(
                json.loads((task_dir / "task.json").read_text())["attempts"][-1][
                    "failure_class"
                ],
                "session-missing",
            )
            self.assertFalse((task_dir / "cooldown.txt").exists())
            cooldown_path = te.state_dir / "cooldowns.json"
            if cooldown_path.exists():
                data = json.loads(cooldown_path.read_text())
                self.assertNotIn("claude", data)

    def test_session_missing_beats_auth_when_a_log_says_both(self):
        """Ordering in classify_failure is load-bearing, not decorative.

        A future CLI might say "no conversation found, please log in". Auth
        benches the worker for every other task; a stale id is not the
        worker's fault. This test reddens if the two checks are swapped.
        """
        adapters = load_ssa("adapters")
        both = "No conversation found with session ID: x. Please log in."
        self.assertEqual(adapters.classify_failure(1, both), "session-missing")
        self.assertNotEqual(adapters.classify_failure(1, both), "auth")
        self.assertEqual(
            adapters.classify_failure(1, "session not found"), "session-missing"
        )
        self.assertEqual(
            adapters.classify_failure(1, "no session found"), "session-missing"
        )
        self.assertEqual(
            adapters.classify_failure(1, "please log in again"), "auth"
        )

    def test_a_live_resume_argv_carries_the_session_id_and_the_agent_pins(self):
        with temp_env() as te:
            repo, task_dir, rc, out, err = _dispatch_resume(
                te, LIVE_SID, {"exit_code": 0, "session_id": LIVE_SID}
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-claude" / "argv.txt")
            self.assertEqual(argv, claude_resume_argv(repo / "BRIEF.md", LIVE_SID))

    def test_a_working_resume_keeps_the_same_session_id(self):
        with temp_env() as te:
            repo, task_dir, rc, out, err = _dispatch_resume(
                te, LIVE_SID, {"exit_code": 0, "session_id": LIVE_SID}
            )
            self.assertEqual(rc, 0, err)
            self.assertEqual((task_dir / "session-id.txt").read_text().strip(), LIVE_SID)
            self.assertFalse((task_dir / "resume-unavailable.txt").exists())


if __name__ == "__main__":
    unittest.main()
