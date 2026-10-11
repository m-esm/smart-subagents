from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (  # noqa: E402
    BIN_DIR,
    make_git_repo,
    run_ssa,
    temp_env,
)
from test_shell import find_only_task_dir, make_task_dir, write_usage_stub  # noqa: E402


def _rec(primary: str = "codex") -> dict:
    args = {
        "codex": ["-c", "model_reasoning_effort=high"],
        "grok": ["--reasoning-effort", "high"],
    }
    ranked = [{"cli": primary, "score": 90}]
    for name in args:
        if name != primary:
            ranked.append({"cli": name, "score": 40})
    return {
        "primary_worker": primary,
        "fallback_workers": [n for n in args if n != primary],
        "local_labor_ok": True,
        "task_size": "medium",
        "task_kind": "default",
        "difficulty": "routine",
        "worker_args": args,
        "ranked": ranked,
        "reasons": [],
    }


def _run_env(te, primary: str = "codex") -> dict:
    stub = te.root / "usage-stub.py"
    write_usage_stub(stub, _rec(primary))
    env = dict(te.env)
    env["SSA_USAGE_PY"] = str(stub)
    env["SSA_STUB_ARGV"] = str(te.root / "usage-argv.txt")
    env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")
    env["GROK_BIN"] = str(BIN_DIR / "fake-grok")
    return env


def _wait_for(path: Path, seconds: float = 10.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


class BaselineTests(unittest.TestCase):
    def test_baseline_writes_results_and_log(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (task_dir / "verify-cmds.txt").write_text("true\nfalse\n")

            rc, out, err = run_ssa("baseline", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 0, err)
            rows = (task_dir / "baseline-results.txt").read_text().splitlines()
            self.assertEqual(rows, ["0\ttrue", "1\tfalse"])
            self.assertTrue((task_dir / "baseline.log").is_file())
            self.assertIn("baseline: exit=0  true", out)
            self.assertIn("baseline: exit=1  false", out)

    def test_baseline_missing_verify_cmds_dies(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (task_dir / "verify-cmds.txt").unlink()

            rc, out, err = run_ssa("baseline", "--dir", str(task_dir), env=te.env)
            self.assertNotEqual(rc, 0)
            self.assertIn("verify-cmds.txt", err)

    def test_baseline_missing_worktree_dies(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (task_dir / "wt.txt").write_text(str(te.root / "gone") + "\n")

            rc, out, err = run_ssa("baseline", "--dir", str(task_dir), env=te.env)
            self.assertNotEqual(rc, 0)
            self.assertIn("worktree", err)


class WaitTests(unittest.TestCase):
    def test_wait_for_verified_after_background_dispatch(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            env = _run_env(te)

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex",
                "--background", env=env,
            )
            self.assertEqual(rc, 0, err + out)
            self.assertTrue(_wait_for(task_dir / "task.json"), "no task.json")

            rc, out, err = run_ssa(
                "wait", "--dir", str(task_dir), "--for", "verified",
                "--timeout", "30", "--interval", "1", env=env,
            )
            self.assertEqual(rc, 0, err + out)
            self.assertTrue((task_dir / "outcome.json").is_file())
            self.assertIn("wait: state=", out)
            self.assertLess(len(out), 6000, out)

    def test_wait_times_out_on_a_live_worker(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                (task_dir / "worker.pid").write_text("%d\n" % sleeper.pid)
                (task_dir / "worker-start.txt").write_text("\n")
                (task_dir / "task.json").write_text(
                    json.dumps({"state": "running"}, indent=2) + "\n"
                )

                rc, out, err = run_ssa(
                    "wait", "--dir", str(task_dir), "--for", "verified",
                    "--timeout", "1", "--interval", "1", env=te.env,
                )
                self.assertEqual(rc, 124, err + out)
                self.assertIn("state=running", out)
                self.assertIn("resume:", out)
                self.assertLess(len(out), 6000, out)
            finally:
                sleeper.terminate()
                sleeper.wait()

    def test_wait_returns_1_on_a_stalled_task(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (task_dir / "task.json").write_text(
                json.dumps({"state": "stalled"}, indent=2) + "\n"
            )
            (task_dir / "stalled.txt").write_text("stalled\n")

            rc, out, err = run_ssa(
                "wait", "--dir", str(task_dir), "--timeout", "5", env=te.env
            )
            self.assertEqual(rc, 1, err + out)
            self.assertIn("state=stalled", out)

    def test_wait_without_task_json_dies(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)

            rc, out, err = run_ssa("wait", "--dir", str(task_dir), env=te.env)
            self.assertNotEqual(rc, 0)
            self.assertIn("task.json", err)


class RunTests(unittest.TestCase):
    def _brief(self, te) -> Path:
        brief = te.root / "brief.md"
        brief.write_text(
            "Do the fixture task.\n\n"
            "Workdir: {WT}\n\n"
            "## Structural discovery\n"
            "CGC-SKIP: fixture; route=none; evidence=run-test\n"
        )
        return brief

    def test_run_end_to_end(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            brief = self._brief(te)
            env = _run_env(te)

            rc, out, err = run_ssa(
                "run", "--repo", str(repo), "--brief", str(brief),
                "--verify-cmd", "true", "--scope", "**", env=env,
            )
            self.assertEqual(rc, 0, err + out)

            task_dir = find_only_task_dir(te.work_dir)
            wt = Path((task_dir / "wt.txt").read_text().strip())
            text = (task_dir / "brief.md").read_text()
            self.assertIn(str(wt), text)
            self.assertNotIn("{WT}", text)
            self.assertTrue((task_dir / "verify-cmds.txt").is_file())
            self.assertEqual((task_dir / "verify-cmds.txt").read_text().strip(), "true")
            self.assertTrue((task_dir / "scope.txt").is_file())
            self.assertTrue((task_dir / "baseline-results.txt").is_file())
            self.assertTrue((task_dir / "outcome.json").is_file())
            self.assertIn(str(task_dir), out.strip().splitlines()[-1])
            self.assertLess(len(out), 6000, out)

    def test_run_no_wait_returns_after_dispatch(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            brief = self._brief(te)
            env = _run_env(te)

            rc, out, err = run_ssa(
                "run", "--repo", str(repo), "--brief", str(brief),
                "--verify-cmd", "true", "--no-wait", env=env,
            )
            self.assertEqual(rc, 0, err + out)

            task_dir = find_only_task_dir(te.work_dir)
            self.assertIn(str(task_dir), out.strip().splitlines()[-1])
            state = json.loads((task_dir / "task.json").read_text())["state"]
            self.assertIn(
                state,
                ("running", "exited", "verified", "failed", "inconclusive", "reported"),
            )
            _wait_for(task_dir / "outcome.json")

    def test_run_without_verify_command_dies_before_init(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            brief = self._brief(te)
            env = _run_env(te)

            rc, out, err = run_ssa(
                "run", "--repo", str(repo), "--brief", str(brief), env=env
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("verify", err)
            self.assertEqual([p for p in te.work_dir.iterdir()], [])


if __name__ == "__main__":
    unittest.main()
