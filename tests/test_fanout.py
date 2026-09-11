"""kind.txt=fanout emits $DIR/harness.js that fans N child task dirs.

Fanout is a dispatch-side kind, not a worker argv mode. dispatch writes the
harness and exits 0; it does not run node, launch a worker, or spawn
Task/Agent. Cap is limits.txt concurrent else 20. N=0 and a missing
fanout.txt refuse nonzero with distinct messages.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (  # noqa: E402
    BIN_DIR,
    make_git_repo,
    run_ssa,
    temp_env,
)
from test_budget import CLAUDE_WORKER_ARGS  # noqa: E402
from test_shell import make_task_dir  # noqa: E402

TASK_RE = re.compile(r"\bTask\b")
AGENT_RE = re.compile(r"\bAgent\b")
SUBAGENT_TYPE_RE = re.compile(r"\bsubagent_type\b")
CAP_RE = re.compile(r"\bCAP\b\s*=\s*(\d+)")


def _claude_env(te):
    env = dict(te.env)
    env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
    env["SSA_ALLOW_UNSANDBOXED_WRITE"] = "1"
    return env


def _dispatch_fanout(te, task_dir, with_worker=False):
    args = ["dispatch", "--dir", str(task_dir)]
    env = _claude_env(te)
    if with_worker:
        args.extend(["--worker", "claude"])
    return run_ssa(*args, env=env)


def _write_fanout(task_dir, paths):
    (task_dir / "fanout.txt").write_text("".join("%s\n" % p for p in paths))


def _baked_cap(src):
    match = CAP_RE.search(src)
    if not match:
        raise AssertionError("no baked CAP integer in harness:\n%s" % src)
    return int(match.group(1))


def _argv_recorder(te):
    return te.home / ".ssa-test" / "fake-claude" / "argv.txt"


class FanoutHarnessTests(unittest.TestCase):
    def test_fanout_harness_calls_ssa_dispatch_not_task_or_agent(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir, repo, kind="fanout", worker_args=CLAUDE_WORKER_ARGS
            )
            children = [
                te.root / "child-a",
                te.root / "child-b",
                te.root / "child-c",
            ]
            _write_fanout(task_dir, children)

            rc, out, err = _dispatch_fanout(te, task_dir, with_worker=True)
            self.assertEqual(rc, 0, err + out)

            harness = task_dir / "harness.js"
            self.assertTrue(harness.is_file(), "dispatch must write $DIR/harness.js")
            src = harness.read_text()
            self.assertIn("smart-subagents.sh", src)
            self.assertIn("dispatch", src)
            self.assertIn("--dir", src)
            for child in children:
                self.assertIn(str(child), src)
            self.assertIsNone(TASK_RE.search(src), src)
            self.assertIsNone(AGENT_RE.search(src), src)
            self.assertIsNone(SUBAGENT_TYPE_RE.search(src), src)
            self.assertEqual(_baked_cap(src), 20)
            self.assertFalse(
                _argv_recorder(te).exists(),
                "fanout must not launch a worker",
            )

    def test_fanout_cap_honors_limits_concurrent_else_20(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            child = te.root / "child-a"

            capped = make_task_dir(te.work_dir, repo, kind="fanout")
            _write_fanout(capped, [child])
            (capped / "limits.txt").write_text("concurrent=2\n")
            rc, out, err = _dispatch_fanout(te, capped)
            self.assertEqual(rc, 0, err + out)
            self.assertEqual(_baked_cap((capped / "harness.js").read_text()), 2)

            defaulted = make_task_dir(te.work_dir, repo, kind="fanout")
            _write_fanout(defaulted, [child])
            rc, out, err = _dispatch_fanout(te, defaulted)
            self.assertEqual(rc, 0, err + out)
            self.assertEqual(_baked_cap((defaulted / "harness.js").read_text()), 20)

            zero = make_task_dir(te.work_dir, repo, kind="fanout")
            _write_fanout(zero, [child])
            (zero / "limits.txt").write_text("concurrent=0\n")
            rc, out, err = _dispatch_fanout(te, zero)
            self.assertNotEqual(rc, 0)
            self.assertIn("concurrent", err)
            self.assertFalse((zero / "harness.js").exists())

    def test_fanout_n_zero_refuses_nonzero(self):
        cases = (
            ("empty", ""),
            ("comments-only", "# child-a\n\n  \n# still a comment\n"),
        )
        for label, text in cases:
            with self.subTest(label=label):
                with temp_env() as te:
                    repo = make_git_repo(te.root / "repo")
                    task_dir = make_task_dir(te.work_dir, repo, kind="fanout")
                    (task_dir / "fanout.txt").write_text(text)
                    rc, out, err = _dispatch_fanout(te, task_dir)
                    self.assertNotEqual(rc, 0)
                    combined = (err + out).lower()
                    self.assertTrue(
                        "n=0" in combined or "empty" in combined,
                        "N=0 refuse must name empty or N=0; got %r" % (err + out),
                    )
                    self.assertFalse((task_dir / "harness.js").exists())

    def test_fanout_missing_list_refuses_nonzero(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, kind="fanout")
            self.assertFalse((task_dir / "fanout.txt").exists())
            rc, out, err = _dispatch_fanout(te, task_dir)
            self.assertNotEqual(rc, 0)
            self.assertIn("fanout.txt", err)
            combined = (err + out).lower()
            self.assertNotIn("n=0", combined)
            self.assertNotIn("empty", combined)
            self.assertFalse((task_dir / "harness.js").exists())


if __name__ == "__main__":
    unittest.main()
