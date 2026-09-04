"""Characterization tests for scripts/smart-subagents.sh, driven entirely
through subprocess calls against the real shell script with fake worker
binaries (tests/fixtures/bin/) and a Python usage-CLI stub (this file, same
pattern tests/smoke.sh already uses). No real worker CLI, no network, no
real credentials.

The dispatch argv assertions in DispatchArgvTests are the contract a Phase
3b runtime/registry migration must preserve byte-for-byte: they assert the
FULL argv list each fake binary received, not just a substring.
"""

import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (  # noqa: E402
    BIN_DIR,
    FIXTURE_BRIEF,
    ROOT,
    SSA_SH,
    make_git_repo,
    read_argv_file,
    run_ssa,
    run_ssa_cli,
    run_ssa_cli_stdin,
    temp_env,
)


def write_usage_stub(path: Path, recommendation: dict) -> None:
    """A stub ai-cli-usage.py: records its own argv, prints a fixed usage doc.

    Mirrors the inline stub tests/smoke.sh builds with a heredoc. Requires
    SSA_STUB_ARGV in the environment (TempEnv-based tests set it per call).
    The recommendation is embedded as a JSON *string literal* (via a nested
    json.dumps) so it round-trips through json.loads at run time rather than
    being spliced in as JSON syntax masquerading as Python source.
    """
    embedded = json.dumps(json.dumps(recommendation))
    script = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "\n"
        'with open(os.environ["SSA_STUB_ARGV"], "w") as out:\n'
        '    out.write("\\n".join(sys.argv[1:]) + "\\n")\n'
        f"recommendation = json.loads({embedded})\n"
        'print(json.dumps({"clis": [], "recommendation": recommendation}))\n'
    )
    path.write_text(script)
    path.chmod(0o755)


def make_task_dir(
    work_dir: Path,
    repo: Path,
    size: str = "medium",
    difficulty: str = "routine",
    kind: str = "default",
    worker_args=None,
    brief_text: str = "Complete the fixture task.\n\n## Structural discovery\nCGC-SKIP: fixture; route=none; evidence=characterization-test\n",
) -> Path:
    """A minimal task dir, built by hand (no cmd_init), matching what
    cmd_dispatch alone requires: wt.txt, base-sha.txt, size.txt,
    worker-args.txt, brief.md.
    """
    task_id = f"test-{uuid.uuid4().hex[:8]}"
    d = work_dir / task_id
    d.mkdir(parents=True)
    (d / "task-id.txt").write_text(task_id + "\n")
    (d / "repo.txt").write_text(str(repo) + "\n")
    (d / "wt.txt").write_text(str(repo) + "\n")
    base_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    (d / "base-sha.txt").write_text(base_sha + "\n")
    (d / "size.txt").write_text(size + "\n")
    (d / "difficulty.txt").write_text(difficulty + "\n")
    (d / "kind.txt").write_text(kind + "\n")
    (d / "worker-args.txt").write_text(
        "\n".join(worker_args or []) + ("\n" if worker_args else "")
    )
    (d / "brief.md").write_text(brief_text)
    return d


def write_control(home: Path, exit_code: int = 0, session_id=None) -> None:
    """Steer a fake worker binary's exit code and session id.

    session_id=None leaves the binary's own default (a synthetic id);
    session_id="" makes it emit no session id at all.
    """
    payload = {"exit_code": exit_code}
    if session_id is not None:
        payload["session_id"] = session_id
    (home / ".ssa-test-control.json").write_text(json.dumps(payload))


def wait_for_background_dispatch(task_dir: Path, seconds: int = 60) -> str:
    """Block until the detached wrapper printed its report line.

    A test that tears its temp dir down on the first artifact races the rest
    of the run's writes.
    """
    log = task_dir / "bg.log"
    deadline = time.time() + seconds
    while time.time() < deadline:
        text = log.read_text(errors="replace") if log.exists() else ""
        if "smart-subagents: worker=" in text:
            time.sleep(0.4)
            return text
        time.sleep(0.2)
    return log.read_text(errors="replace") if log.exists() else ""


def find_only_task_dir(work_dir: Path) -> Path:
    # work_dir/wt holds the worktrees themselves (siblings of the task dirs, so
    # a worker cannot reach ../scope.txt from its cwd), never a task dir.
    dirs = [p for p in work_dir.iterdir() if p.is_dir() and p.name != "wt"]
    assert len(dirs) == 1, f"expected exactly one task dir under {work_dir}, got {dirs}"
    return dirs[0]


class InitTests(unittest.TestCase):
    def test_init_writes_expected_artifacts_and_plumbs_flags_to_usage(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            stub = te.root / "usage-stub.py"
            argv_file = te.root / "usage-argv.txt"
            write_usage_stub(
                stub,
                {
                    "primary_worker": "codex",
                    "fallback_workers": ["grok"],
                    "local_labor_ok": True,
                    "task_size": "medium",
                    "task_kind": "review",
                    "difficulty": "hard",
                    "target_effort": "high",
                    "cross_review_required": False,
                    "worker_args": {
                        "codex": ["-c", "model_reasoning_effort=high"],
                        "grok": ["--reasoning-effort", "high"],
                        "kimi": [],
                    },
                    "ranked": [{"cli": "codex", "score": 80}, {"cli": "grok", "score": 60}],
                    "reasons": [],
                },
            )
            env = dict(te.env)
            env["SSA_USAGE_PY"] = str(stub)
            env["SSA_STUB_ARGV"] = str(argv_file)

            rc, out, err = run_ssa(
                "init", "--repo", str(repo), "--kind", "review", "--difficulty", "hard", env=env
            )
            self.assertEqual(rc, 0, err)

            task_dir = find_only_task_dir(te.work_dir)
            for name in (
                "task-id.txt",
                "repo.txt",
                "size.txt",
                "difficulty.txt",
                "kind.txt",
                "base-sha.txt",
                "repo-branch.txt",
                "repo-status.txt",
                "usage.json",
                "wt.txt",
                "worker-args.txt",
                "pick.json",
                "worker.txt",
            ):
                self.assertTrue((task_dir / name).exists(), f"missing {name}")

            self.assertEqual((task_dir / "kind.txt").read_text().strip(), "review")
            self.assertEqual((task_dir / "difficulty.txt").read_text().strip(), "hard")
            self.assertEqual((task_dir / "worker.txt").read_text().strip(), "codex")
            self.assertEqual(
                (task_dir / "worker-args-codex.txt").read_text().strip(),
                "-c\nmodel_reasoning_effort=high",
            )
            self.assertEqual(
                (task_dir / "worker-args-grok.txt").read_text().strip(),
                "--reasoning-effort\nhigh",
            )

            argv = read_argv_file(argv_file)
            self.assertIn("--task-size", argv)
            self.assertIn("--difficulty", argv)
            self.assertIn("hard", argv)
            self.assertIn("--task-kind", argv)
            self.assertIn("review", argv)

            wt = Path((task_dir / "wt.txt").read_text().strip())
            self.assertTrue(wt.is_dir())
            branch = subprocess.check_output(
                ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], text=True
            ).strip()
            task_id = (task_dir / "task-id.txt").read_text().strip()
            self.assertEqual(branch, f"ssa/{task_id}")


class DispatchArgvTests(unittest.TestCase):
    """Locks the exact argv each worker binary receives. Phase 3b's runtime
    migration must reproduce these byte-for-byte or update them on purpose.
    """

    def test_codex_receives_exact_argv_and_brief_on_stdin(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            brief_text = "Complete the fixture task.\n\n## Structural discovery\nCGC-SKIP: fixture; route=none; evidence=characterization-test\n"
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                worker_args=["-c", "model_reasoning_effort=high"],
                brief_text=brief_text,
            )
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")

            rc, out, err = run_ssa("dispatch", "--dir", str(task_dir), "--worker", "codex", env=env)
            self.assertEqual(rc, 0, err)

            recorder = te.home / ".ssa-test" / "fake-codex"
            argv = read_argv_file(recorder / "argv.txt")
            expected = [
                "exec",
                "-C",
                str(repo),
                "-s",
                "workspace-write",
                "--json",
                "-c",
                "model_reasoning_effort=high",
                "-o",
                str(task_dir / "last-msg.txt"),
                "-",
            ]
            self.assertEqual(argv, expected)
            self.assertEqual((recorder / "stdin.txt").read_text(), brief_text)

            self.assertEqual((task_dir / "exit-code.txt").read_text().strip(), "0")
            sid = (task_dir / "session-id.txt").read_text().strip()
            self.assertTrue(sid)
            self.assertFalse((task_dir / "resume-unavailable.txt").exists())

    def test_codex_missing_session_id_writes_resume_unavailable(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            write_control(te.home, session_id="")
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")

            rc, out, err = run_ssa("dispatch", "--dir", str(task_dir), "--worker", "codex", env=env)
            self.assertEqual(rc, 0, err)
            self.assertEqual((task_dir / "session-id.txt").read_text().strip(), "")
            self.assertEqual(
                (task_dir / "resume-unavailable.txt").read_text().strip(),
                "worker did not emit a resumable session id",
            )

    def test_grok_receives_exact_argv(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            brief_text = "Complete the fixture task.\n\n## Structural discovery\nCGC-SKIP: fixture; route=none; evidence=characterization-test\n"
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                worker_args=["--reasoning-effort", "high"],
                brief_text=brief_text,
            )
            env = dict(te.env)
            env["GROK_BIN"] = str(BIN_DIR / "fake-grok")

            rc, out, err = run_ssa("dispatch", "--dir", str(task_dir), "--worker", "grok", env=env)
            self.assertEqual(rc, 0, err)

            recorder = te.home / ".ssa-test" / "fake-grok"
            argv = read_argv_file(recorder / "argv.txt")
            expected = [
                "-p",
                f"Read the file {repo / 'BRIEF.md'} and complete the task it describes.",
                "--cwd",
                str(repo),
                "--sandbox",
                "workspace",
                "--reasoning-effort",
                "high",
                "--output-format",
                "streaming-messages-json",
            ]
            self.assertEqual(argv, expected)
            self.assertEqual((task_dir / "exit-code.txt").read_text().strip(), "0")
            self.assertTrue((task_dir / "session-id.txt").read_text().strip())
            # grok has no -o flag: dispatch fills last-msg.txt from the log.
            self.assertEqual(
                (task_dir / "last-msg.txt").read_text().strip(), "fake-grok final: ok"
            )
            # The dispatch report itself stays small and carries the final text.
            self.assertLess(len(out), 2500, out)
            self.assertIn("fake-grok final: ok", out)

    def test_dispatch_rebinds_stale_claude_args_when_overriding_to_grok(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            brief_text = "Complete the fixture task.\n\n## Structural discovery\nCGC-SKIP: fixture; route=none; evidence=characterization-test\n"
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                worker_args=["--effort", "high", "--model", "fable"],
                brief_text=brief_text,
            )
            (task_dir / "worker.txt").write_text("claude\n")
            (task_dir / "pick.json").write_text(
                json.dumps(
                    {
                        "worker": "claude",
                        "all_worker_args": {
                            "claude": ["--effort", "high", "--model", "fable"],
                            "grok": ["--reasoning-effort", "high"],
                        },
                    }
                )
            )
            env = dict(te.env)
            env["GROK_BIN"] = str(BIN_DIR / "fake-grok")

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "grok", env=env
            )
            self.assertEqual(rc, 0, err)

            argv = read_argv_file(te.home / ".ssa-test" / "fake-grok" / "argv.txt")
            self.assertIn("--reasoning-effort", argv)
            self.assertIn("high", argv)
            self.assertNotIn("--model", argv)
            self.assertNotIn("fable", argv)
            self.assertEqual(
                (task_dir / "worker-args.txt").read_text().strip(),
                "--reasoning-effort\nhigh",
            )

    def test_dispatch_refuses_foreign_flags_when_override_has_no_args_table(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                worker_args=["--effort", "high", "--model", "fable"],
            )
            (task_dir / "pick.json").write_text(
                json.dumps(
                    {
                        "worker": "claude",
                        "all_worker_args": {
                            "claude": ["--effort", "high", "--model", "fable"],
                        },
                    }
                )
            )
            env = dict(te.env)
            env["GROK_BIN"] = str(BIN_DIR / "fake-grok")

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "grok", env=env
            )
            self.assertNotEqual(rc, 0, err)
            self.assertIn("worker-args", err)
            self.assertIn("claude", err)

    def test_kimi_write_dispatch_receives_exact_argv_and_runs_in_worktree(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            env = dict(te.env)
            env["KIMI_BIN"] = str(BIN_DIR / "fake-kimi")
            env["SSA_ALLOW_KIMI_WRITE"] = "1"

            rc, out, err = run_ssa("dispatch", "--dir", str(task_dir), "--worker", "kimi", env=env)
            self.assertEqual(rc, 0, err)

            recorder = te.home / ".ssa-test" / "fake-kimi"
            argv = read_argv_file(recorder / "argv.txt")
            brief_path = repo / "BRIEF.md"
            expected = [
                "-p",
                f"Read the file {brief_path} and complete the task it describes.",
                "--output-format",
                "stream-json",
            ]
            self.assertEqual(argv, expected)

            recorded_cwd = (recorder / "cwd.txt").read_text().strip()
            self.assertEqual(os.path.realpath(recorded_cwd), os.path.realpath(str(repo)))

            self.assertTrue((task_dir / "kimi-override.txt").exists())
            self.assertEqual((task_dir / "exit-code.txt").read_text().strip(), "0")
            self.assertTrue((task_dir / "session-id.txt").read_text().strip())

    def test_kimi_write_dispatch_with_highspeed_alias_flag(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir, repo, worker_args=["-m", "kimi-for-coding-highspeed"]
            )
            env = dict(te.env)
            env["KIMI_BIN"] = str(BIN_DIR / "fake-kimi")
            env["SSA_ALLOW_KIMI_WRITE"] = "1"

            rc, out, err = run_ssa("dispatch", "--dir", str(task_dir), "--worker", "kimi", env=env)
            self.assertEqual(rc, 0, err)
            recorder = te.home / ".ssa-test" / "fake-kimi"
            argv = read_argv_file(recorder / "argv.txt")
            brief_path = repo / "BRIEF.md"
            expected = [
                "-p",
                f"Read the file {brief_path} and complete the task it describes.",
                "--output-format",
                "stream-json",
                "-m",
                "kimi-for-coding-highspeed",
            ]
            self.assertEqual(argv, expected)


class PlanTests(unittest.TestCase):
    def test_plan_round_robins_planners_across_ranked_workers(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            stub = te.root / "usage-stub.py"
            write_usage_stub(
                stub,
                {
                    "primary_worker": "codex",
                    "fallback_workers": ["grok"],
                    "ranked": [{"cli": "codex", "score": 80}, {"cli": "grok", "score": 60}],
                    "worker_args": {
                        "codex": ["-c", "model_reasoning_effort=high"],
                        "grok": ["--reasoning-effort", "high"],
                        "kimi": [],
                    },
                    "reasons": [],
                },
            )
            env = dict(te.env)
            env["SSA_USAGE_PY"] = str(stub)
            env["SSA_STUB_ARGV"] = str(te.root / "usage-argv.txt")
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")
            env["GROK_BIN"] = str(BIN_DIR / "fake-grok")

            rc, out, err = run_ssa(
                "plan",
                "--repo",
                str(repo),
                "--n",
                "3",
                "--difficulty",
                "hard",
                "--goal",
                "Plan the fixture.",
                env=env,
            )
            self.assertEqual(rc, 0, err)
            doc = json.loads(out)

            # index i % 2: pragmatic->codex, risk->grok, architecture->codex
            self.assertEqual(
                doc["planners"], ["pragmatic:codex", "risk:grok", "architecture:codex"]
            )
            self.assertEqual(len(doc["plans"]), 3)
            for plan in doc["plans"]:
                self.assertFalse(plan["empty"], plan)
                self.assertGreater(plan["bytes"], 0)

            plan_dir = Path(doc["dir"])
            brief0 = (plan_dir / "brief-0-pragmatic.md").read_text()
            self.assertIn("## Goal", brief0)
            self.assertIn("Plan the fixture.", brief0)
            self.assertIn("smallest correct change", brief0)
            brief1 = (plan_dir / "brief-1-risk.md").read_text()
            self.assertIn("what breaks", brief1)

            # Effort flags applied per worker, recorded in worker-args-<cli>.txt.
            self.assertEqual(
                (plan_dir / "worker-args-codex.txt").read_text().strip(),
                "-c\nmodel_reasoning_effort=high",
            )
            self.assertEqual(
                (plan_dir / "worker-args-grok.txt").read_text().strip(),
                "--reasoning-effort\nhigh",
            )

            codex_recorder = te.home / ".ssa-test" / "fake-codex"
            codex_argv = read_argv_file(codex_recorder / "argv.txt")
            self.assertIn("-s", codex_argv)
            self.assertIn("read-only", codex_argv)
            self.assertIn("-c", codex_argv)
            self.assertIn("model_reasoning_effort=high", codex_argv)

            grok_recorder = te.home / ".ssa-test" / "fake-grok"
            grok_argv = read_argv_file(grok_recorder / "argv.txt")
            self.assertIn("--reasoning-effort", grok_argv)
            self.assertIn("high", grok_argv)
            self.assertIn("--sandbox", grok_argv)
            self.assertIn("workspace", grok_argv)


class VerifyTests(unittest.TestCase):
    def test_verify_pass_fail_and_inconclusive(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)

            # pass: command succeeds, baseline agrees.
            (task_dir / "verify-cmds.txt").write_text("true\n")
            (task_dir / "baseline-results.txt").write_text("0\ttrue\n")
            rc, out, err = run_ssa("verify", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 0, err)
            doc = json.loads((task_dir / "outcome.json").read_text())
            self.assertEqual(doc["verify"]["verdict"], "pass")

            # fail: a command regresses relative to baseline.
            (task_dir / "verify-cmds.txt").write_text("true\nfalse\n")
            (task_dir / "baseline-results.txt").write_text("0\ttrue\n0\tfalse\n")
            rc, out, err = run_ssa("verify", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 1, err)
            doc = json.loads((task_dir / "outcome.json").read_text())
            self.assertEqual(doc["verify"]["verdict"], "fail")
            self.assertEqual(doc["verify"]["new_failures"], 1)

            # inconclusive: a failure with no baseline to compare against.
            (task_dir / "baseline-results.txt").unlink()
            rc, out, err = run_ssa("verify", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 2, err)
            doc = json.loads((task_dir / "outcome.json").read_text())
            self.assertEqual(doc["verify"]["verdict"], "inconclusive")

    def test_verify_empty_tree_with_brief_permission_denial_is_not_pass(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (task_dir / "verify-cmds.txt").write_text("true\n")
            (task_dir / "baseline-results.txt").write_text("0\ttrue\n")
            (task_dir / "stdout.log").write_text(
                "EACCES: permission denied, open '%s/brief.md'\n" % task_dir
            )
            rc, out, err = run_ssa("verify", "--dir", str(task_dir), env=te.env)
            self.assertNotEqual(rc, 0, err)
            doc = json.loads((task_dir / "outcome.json").read_text())
            self.assertNotEqual(doc["verify"]["verdict"], "pass")
            self.assertEqual(doc["verify"]["verdict"], "fail")
            self.assertEqual(doc["verify"]["changed_files"], 0)


class WatchdogTests(unittest.TestCase):
    def test_background_dispatch_does_not_stall_after_worker_exits(self):
        # 1788154673-70654 / 1788215189-74491: grok exited, supervisor
        # recorded, then the watchdog (TERM ignored, leader=bg-run wrapper)
        # stamped stalled and attempted reported -> stalled.
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")
            env["SSA_STALL_SECS"] = "2"
            env["SSA_WATCHDOG_INTERVAL_SECS"] = "1"
            rc, out, err = run_ssa(
                "dispatch",
                "--dir",
                str(task_dir),
                "--worker",
                "codex",
                "--background",
                env=env,
            )
            self.assertEqual(rc, 0, err + out)
            deadline = time.time() + 15
            while time.time() < deadline:
                if (task_dir / "exit-code.txt").exists():
                    break
                time.sleep(0.1)
            else:
                self.fail("background worker never wrote exit-code.txt\n" + err + out)
            time.sleep(3.5)
            bg = ""
            if (task_dir / "bg.log").exists():
                bg = (task_dir / "bg.log").read_text()
            self.assertFalse(
                (task_dir / "stalled.txt").exists(),
                "watchdog stalled a finished dispatch:\n" + bg,
            )
            self.assertNotIn("illegal transition", bg)


class LogDigestTests(unittest.TestCase):
    """stdout.log is for the disk. Measured 2026-09-01: grok implement logs run
    1-3 MB with single NDJSON lines up to 186 KB. `status` used to `tail -n3`
    those raw lines straight into the supervisor context."""

    def _task_with_big_log(self, te, worker="grok"):
        repo = make_git_repo(te.root / "repo")
        task_dir = make_task_dir(te.work_dir, repo)
        (task_dir / "worker.txt").write_text(worker + "\n")
        (task_dir / "exit-code.txt").write_text("0\n")
        big = "y" * 200_000
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s" * 36}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": big, "signature": big},
                {"type": "tool_use", "name": "read_file", "input": {"target_file": "a.py"}},
            ]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": big},
            ]}}),
            json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": big}}),
            json.dumps({"type": "result", "subtype": "success", "num_turns": 3,
                        "result": "FINAL-MARKER " + "z" * 5000}),
        ]
        (task_dir / "stdout.log").write_text("\n".join(lines) + "\n")
        return task_dir

    def test_status_output_is_bounded_regardless_of_log_line_size(self):
        with temp_env() as te:
            task_dir = self._task_with_big_log(te)
            rc, out, err = run_ssa("status", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertLess(len(out), 4000, "status leaked raw log lines: %d bytes" % len(out))
            self.assertIn("FINAL-MARKER", out)
            self.assertIn("tool read_file", out)
            self.assertNotIn("y" * 500, out)

    def test_tail_is_filtered_by_default_and_raw_on_request(self):
        with temp_env() as te:
            task_dir = self._task_with_big_log(te)
            # tail -f never exits on its own; feed the filter directly instead
            # and prove the shell wires the same seam.
            rc, out, err = run_ssa_cli_stdin(
                (task_dir / "stdout.log").read_text(), "tail-filter", env=te.env
            )
            self.assertEqual(rc, 0, err)
            self.assertLess(len(out), 1200, out)
            self.assertIn("tool read_file", out)
            self.assertIn("tool_result 200000 bytes", out)
            self.assertIn("result success turns=3", out)
            sh = SSA_SH.read_text()
            self.assertIn('tail -n "$lines" -f "$dir/stdout.log" | _ssa tail-filter', sh)
            self.assertIn("--raw", sh)

    def test_status_on_unregistered_worker_still_clips(self):
        with temp_env() as te:
            task_dir = self._task_with_big_log(te, worker="nosuchcli")
            rc, out, err = run_ssa("status", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertLess(len(out), 4000, out)
            self.assertIn("unregistered worker", out)


class RecordTests(unittest.TestCase):
    def test_record_writes_ledger_line_without_absolute_paths(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (task_dir / "exit-code.txt").write_text("0\n")

            rc, out, err = run_ssa(
                "record",
                "--dir",
                str(task_dir),
                "--outcome",
                "verified-pass",
                "--retries",
                "1",
                env=te.env,
            )
            self.assertEqual(rc, 0, err)

            ledger_path = te.state_dir / "outcomes.jsonl"
            self.assertTrue(ledger_path.exists())
            ledger_text = ledger_path.read_text()
            self.assertIn('"outcome": "verified-pass"', ledger_text)
            self.assertNotIn(str(repo), ledger_text)
            self.assertNotIn(str(task_dir), ledger_text)
            self.assertNotIn(str(te.work_dir), ledger_text)

            record = json.loads(ledger_text.strip().splitlines()[-1])
            self.assertEqual(record["outcome"], "verified-pass")
            self.assertEqual(record["retries"], 1)
            self.assertTrue(record["repo_hash"])
            self.assertNotIn("repo", record)
            self.assertNotIn("dir", record)


class StructuralBriefTests(unittest.TestCase):
    def test_dispatch_refuses_brief_without_structural_section(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir, repo, brief_text="Complete the fixture task.\n"
            )
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex", env=env
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("Structural discovery", err)

    def test_dispatch_refuses_missing_pack_file(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                brief_text=(
                    "Go.\n\n## Structural discovery\n"
                    "CGC: /tmp/ssa-missing-pack-does-not-exist.txt\n"
                ),
            )
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex", env=env
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("pack not found", err)

    def test_dispatch_refuses_not_in_graph_stub(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            stub = te.root / "pack.txt"
            stub.write_text("== Foo: NOT IN GRAPH\n")
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                brief_text=f"Go.\n\n## Structural discovery\nCGC: {stub}\n",
            )
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex", env=env
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("miss stub", err)

    def test_dispatch_accepts_cgc_pack_and_records_route(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            pack = te.root / "pack.txt"
            pack.write_text("== Foo  src/foo.py:10-20  [Function]  cx=1  ()\n-- callers: none in graph\n")
            task_dir = make_task_dir(
                te.work_dir,
                repo,
                brief_text=f"Go.\n\n## Structural discovery\nCGC: {pack}\n",
            )
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex", env=env
            )
            self.assertEqual(rc, 0, err)
            self.assertEqual((task_dir / "route.txt").read_text().strip(), "cgc")

    def test_legacy_override_allows_one_line_brief(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(
                te.work_dir, repo, brief_text="Complete the fixture task.\n"
            )
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")
            env["SSA_STRUCTURAL_LEGACY"] = "1"
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex", env=env
            )
            self.assertEqual(rc, 0, err)
            self.assertEqual((task_dir / "route.txt").read_text().strip(), "legacy")



def write_script(path: Path, body: str) -> Path:
    """A throwaway worker binary for one test: no fixture, no registry entry."""
    path.write_text(body)
    path.chmod(0o755)
    return path


def make_plain_task_dir(work_dir: Path, name: str, files: dict) -> Path:
    """A task dir built from literal files, for the listing and gc paths that
    only read artifacts and never launch anything."""
    d = work_dir / name
    d.mkdir(parents=True)
    for filename, text in files.items():
        (d / filename).write_text(text)
    return d


class OutputBoundsTests(unittest.TestCase):
    """Every byte these commands print lands in a supervisor's context.
    Measured 2026-09-01: verify-summary on a real repo emitted 203 KB, almost
    all of it a `git status -uall` listing of untracked files."""

    def test_verify_summary_clips_a_huge_status_and_spills_the_full_text(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            for i in range(300):
                (repo / ("untracked-%03d.txt" % i)).write_text("scratch\n")

            rc, out, err = run_ssa("verify-summary", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertLess(len(out), 20000, "verify-summary leaked the whole status")
            self.assertIn("more lines, full text in", out)
            full = task_dir / "verify-summary-full.txt"
            self.assertTrue(full.exists())
            self.assertIn("untracked-299.txt", full.read_text())
            self.assertNotIn("untracked-299.txt", out)

    def test_diff_prints_a_stat_by_default_and_clips_a_path_diff(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo", {"big.txt": "seed\n"})
            task_dir = make_task_dir(te.work_dir, repo)
            (repo / "big.txt").write_text("".join("line %d\n" % i for i in range(4000)))

            rc, out, err = run_ssa("diff", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertIn("### diff stat", out)
            self.assertIn("big.txt", out)
            self.assertNotIn("line 3999", out)

            rc, out, err = run_ssa(
                "diff", "--dir", str(task_dir), "--path", "big.txt",
                "--max-bytes", "500", env=te.env,
            )
            self.assertEqual(rc, 0, err)
            self.assertIn("(truncated,", out)
            self.assertLess(len(out), 2000, out)

    def test_ls_caps_old_rows_but_never_hides_a_live_task(self):
        with temp_env() as te:
            done = {
                "task-id.txt": "x\n", "repo.txt": str(te.root / "repo") + "\n",
                "brief.md": "b\n", "stdout.log": "l\n", "exit-code.txt": "0\n",
            }
            for i in range(30):
                d = make_plain_task_dir(te.work_dir, "1700000%03d-1" % i, done)
                os.utime(d, (1_700_000_000 + i, 1_700_000_000 + i))
            live = make_plain_task_dir(
                te.work_dir, "1600000000-1",
                {"task-id.txt": "live\n", "repo.txt": str(te.root / "repo") + "\n",
                 "brief.md": "b\n"},
            )
            os.utime(live, (1_600_000_000, 1_600_000_000))

            rc, out, err = run_ssa("ls", env=te.env)
            self.assertEqual(rc, 0, err)
            rows = [l for l in out.splitlines() if l.startswith(("1700", "1600"))]
            self.assertEqual(len(rows), 21, out)
            self.assertIn("1600000000-1", out)
            self.assertIn("older tasks hidden", out)

            rc, out, err = run_ssa("ls", "--all", env=te.env)
            self.assertEqual(rc, 0, err)
            rows = [l for l in out.splitlines() if l.startswith(("1700", "1600"))]
            self.assertEqual(len(rows), 31, out)
            self.assertNotIn("older tasks hidden", out)

            rc, out, err = run_ssa("ls", "--state", "briefed", "--all", env=te.env)
            self.assertEqual(rc, 0, err)
            rows = [l for l in out.splitlines() if l.startswith(("1700", "1600"))]
            self.assertEqual(len(rows), 1, out)
            self.assertIn("1600000000-1", out)

    def test_gc_dry_run_aggregates_kept_rows_by_reason(self):
        with temp_env() as te:
            for i in range(30):
                make_plain_task_dir(
                    te.work_dir, "1799999%03d-1" % i,
                    {"task-id.txt": "x\n", "repo.txt": str(te.root / "repo") + "\n"},
                )
            rc, out, err = run_ssa("gc", "--older-than", "7", env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertIn("kept: 30 younger than 7d", out)
            self.assertLess(len(out.splitlines()), 5, out)

            rc, out, err = run_ssa("gc", "--older-than", "7", "--verbose", env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertEqual(len([l for l in out.splitlines() if l.startswith("kept  ")]), 30)

    def test_pick_summarizes_on_stderr_unless_explain(self):
        with temp_env() as te:
            stub = te.root / "usage-stub.py"
            write_usage_stub(
                stub,
                {
                    "primary_worker": "codex",
                    "fallback_workers": ["grok", "kimi"],
                    "local_labor_ok": True,
                    "ranked": [{"cli": "codex", "score": 80}, {"cli": "grok", "score": 60}],
                    "worker_args": {"codex": [], "grok": []},
                    "reasons": ["one", "two"],
                },
            )
            env = dict(te.env)
            env["SSA_USAGE_PY"] = str(stub)
            env["SSA_STUB_ARGV"] = str(te.root / "usage-argv.txt")

            rc, out, err = run_ssa("pick", "--size", "medium", env=env)
            self.assertEqual(rc, 0, err)
            self.assertEqual(out.strip(), "codex")
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn("primary=codex", err)
            self.assertIn("fallbacks=grok,kimi", err)
            self.assertIn("local_labor_ok=True", err)

            rc, out, err = run_ssa("pick", "--size", "medium", "--explain", env=env)
            self.assertEqual(rc, 0, err)
            self.assertIn('"ranked"', err)

    def test_verify_bounds_the_out_of_scope_list_and_spills_the_rest(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            for i in range(40):
                (repo / ("gen-%02d.txt" % i)).write_text("x\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "worker change"], cwd=repo, check=True
            )
            (task_dir / "scope.txt").write_text("src/**\n")

            rc, out, err = run_ssa("verify", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 1, out + err)
            doc = json.loads((task_dir / "outcome.json").read_text())["verify"]
            self.assertEqual(doc["out_of_scope_count"], 40)
            self.assertEqual(len(doc["out_of_scope"]), 25)
            spill = task_dir / "verify-out-of-scope.txt"
            self.assertTrue(spill.exists())
            self.assertEqual(len(spill.read_text().split()), 40)


class LifecycleTests(unittest.TestCase):
    SLEEPER = "#!/bin/sh\nsleep 30\n"

    def _bg_env(self, te, sleeper):
        env = dict(te.env)
        env["CODEX_BIN"] = str(sleeper)
        env["SSA_STALL_SECS"] = "2"
        env["SSA_WATCHDOG_INTERVAL_SECS"] = "1"
        env["SSA_KILL_GRACE_SECS"] = "2"
        return env

    def _wait_for(self, path, seconds=40):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if path.exists():
                return True
            time.sleep(0.2)
        return False

    def test_a_stale_exit_code_does_not_disarm_the_watchdog(self):
        # The watchdog read exit-code.txt from the PREVIOUS dispatch and exited
        # on its first tick, so every re-dispatch ran unsupervised.
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (task_dir / "exit-code.txt").write_text("0\n")
            sleeper = write_script(te.root / "codex-sleep.sh", self.SLEEPER)
            env = self._bg_env(te, sleeper)

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex",
                "--background", env=env,
            )
            self.assertEqual(rc, 0, err + out)
            self.assertTrue(
                self._wait_for(task_dir / "stalled.txt"),
                "watchdog never stalled a silent worker:\n"
                + (task_dir / "bg.log").read_text(),
            )
            # B6: the wrapper survives the kill, so the run still reports.
            self.assertTrue(self._wait_for(task_dir / "exit-code.txt"))
            self.assertTrue(self._wait_for(task_dir / "diff-stat.txt"))
            wait_for_background_dispatch(task_dir)

    def test_foreground_dispatch_records_a_live_worker_pid(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            sleeper = write_script(te.root / "codex-slow.sh", "#!/bin/sh\nsleep 5\n")
            env = dict(te.env)
            env["CODEX_BIN"] = str(sleeper)

            proc = subprocess.Popen(
                ["bash", str(SSA_SH), "dispatch", "--dir", str(task_dir),
                 "--worker", "codex"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                self.assertTrue(self._wait_for(task_dir / "worker.pid", 20))
                seen = ""
                deadline = time.time() + 20
                while time.time() < deadline:
                    _, seen, _ = run_ssa("status", "--dir", str(task_dir), env=te.env)
                    if "(running)" in seen:
                        break
                    time.sleep(0.2)
                self.assertIn("(running)", seen)
                pid = int((task_dir / "worker.pid").read_text().strip())
                self.assertNotEqual(pid, proc.pid)
                self.assertTrue((task_dir / "worker.pgid").read_text().strip())
            finally:
                proc.wait(timeout=60)

    def test_a_reused_pid_is_not_a_live_worker(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            # A pid that is alive but is not the process we launched.
            (task_dir / "worker.pid").write_text("%d\n" % os.getpid())
            (task_dir / "worker-start.txt").write_text("Thu Jan  1 00:00:00 1970\n")

            rc, out, err = run_ssa("status", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertIn("(reused)", out)

            rc, out, err = run_ssa("gc", "--older-than", "0", env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertIn("safe  task", out)
            self.assertNotIn("is alive", out)

    def test_a_refused_transition_is_recorded_and_the_record_moves_on(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            # The record starts at the state the artifacts imply (preflighted:
            # a worktree exists, no worker picked yet).
            for step in ("picked", "running", "exited", "reported"):
                rc, out, err = run_ssa_cli(
                    "transition", "--dir", str(task_dir), "--to", step, env=te.env
                )
                self.assertEqual(rc, 0, err)
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex", env=env
            )
            self.assertEqual(rc, 0, err)
            marker = task_dir / "state-desync.txt"
            self.assertTrue(marker.exists(), err)
            self.assertIn("reported -> running", marker.read_text())
            record = json.loads((task_dir / "task.json").read_text())
            self.assertTrue(record.get("desync"))
            # The record follows the task instead of freezing at "reported".
            rc, out, err = run_ssa("ls", "--all", env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertRegex(out, r"exited")

    def test_gc_keeps_a_planning_panel_until_it_reports_done(self):
        with temp_env() as te:
            panel = make_plain_task_dir(
                te.work_dir, "plan-1700000000-1",
                {"repo.txt": str(te.root / "repo") + "\n",
                 "plan-0-pragmatic-codex.md": "# plan\n"},
            )
            rc, out, err = run_ssa("gc", "--older-than", "0", env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertNotIn("safe  panel", out)
            self.assertIn("kept:", out)

            (panel / "panel-done.txt").write_text("done\n")
            rc, out, err = run_ssa("gc", "--older-than", "0", env=te.env)
            self.assertEqual(rc, 0, err)
            self.assertIn("safe  panel", out)


class WorktreeBriefTests(unittest.TestCase):
    def test_staged_brief_is_excluded_and_removed_in_a_linked_worktree(self):
        # --absolute-git-dir points at .git/worktrees/<id>, whose info/exclude
        # git never reads, so the brief showed as untracked in every dispatch.
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            wt = te.root / "linked-wt"
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-q", str(wt), "-b", "ssa/x"],
                check=True,
            )
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "wt.txt").write_text(str(wt) + "\n")
            env = dict(te.env)
            env["KIMI_BIN"] = str(BIN_DIR / "fake-kimi")
            env["SSA_ALLOW_KIMI_WRITE"] = "1"

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "kimi", env=env
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-kimi" / "argv.txt")
            self.assertIn(
                "Read the file %s and complete the task it describes." % (wt / "BRIEF.md"),
                argv,
            )
            porcelain = subprocess.check_output(
                ["git", "-C", str(wt), "status", "--porcelain", "-uall"], text=True
            )
            self.assertEqual(porcelain.strip(), "", porcelain)
            self.assertFalse((wt / "BRIEF.md").exists())
            common = subprocess.check_output(
                ["git", "-C", str(wt), "rev-parse", "--git-common-dir"], text=True
            ).strip()
            exclude = Path(common) / "info" / "exclude"
            self.assertIn("/BRIEF.md", exclude.read_text())


class PlanPanelTests(unittest.TestCase):
    SILENT = "#!/bin/sh\nexit 0\n"
    WRITER = (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "argv = sys.argv[1:]\n"
        "wt = argv[argv.index('-C') + 1] if '-C' in argv else os.getcwd()\n"
        "open(os.path.join(wt, 'planner-wrote-this.txt'), 'w').write('oops\\n')\n"
    )

    def _plan_env(self, te, codex_bin):
        stub = te.root / "usage-stub.py"
        write_usage_stub(
            stub,
            {
                "primary_worker": "codex",
                "fallback_workers": [],
                "ranked": [{"cli": "codex", "score": 80}],
                "worker_args": {"codex": []},
                "reasons": [],
            },
        )
        env = dict(te.env)
        env["SSA_USAGE_PY"] = str(stub)
        env["SSA_STUB_ARGV"] = str(te.root / "usage-argv.txt")
        env["CODEX_BIN"] = str(codex_bin)
        return env

    def test_an_empty_planner_gets_a_digest_not_a_path_to_555kb_of_ndjson(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            env = self._plan_env(te, write_script(te.root / "silent.sh", self.SILENT))
            rc, out, err = run_ssa(
                "plan", "--repo", str(repo), "--n", "1", "--goal", "Plan it.", env=env
            )
            self.assertEqual(rc, 0, err)
            doc = json.loads(out)
            plan = doc["plans"][0]
            self.assertTrue(plan["empty"])
            self.assertIn("log_bytes", plan)
            self.assertIn("digest", plan["log_digest_cmd"])
            text = Path(plan["file"]).read_text()
            self.assertIn("planner produced no output", text)
            self.assertIn("log:", text)
            self.assertNotIn("see plan-0.log", text)
            self.assertTrue((Path(doc["dir"]) / "panel-done.txt").exists())

    def test_a_planner_that_writes_marks_the_panel_dirty(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            env = self._plan_env(te, write_script(te.root / "writer.py", self.WRITER))
            rc, out, err = run_ssa(
                "plan", "--repo", str(repo), "--n", "1", "--goal", "Plan it.", env=env
            )
            self.assertEqual(rc, 0, err)
            doc = json.loads(out)
            self.assertTrue(doc.get("dirty"))
            self.assertIn("dirty", err)
            self.assertTrue((Path(doc["dir"]) / "panel-dirty.txt").exists())

    def test_the_panel_worktree_is_not_inside_the_panel_dir(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            env = self._plan_env(te, write_script(te.root / "silent.sh", self.SILENT))
            rc, out, err = run_ssa(
                "plan", "--repo", str(repo), "--n", "1", "--goal", "Plan it.", env=env
            )
            self.assertEqual(rc, 0, err)
            doc = json.loads(out)
            self.assertFalse(doc["worktree"].startswith(doc["dir"] + os.sep))
            self.assertEqual(
                Path(doc["worktree"]).parent, te.work_dir / "wt"
            )


class WorkDirIsolationTests(unittest.TestCase):
    def test_init_puts_the_worktree_beside_the_task_dir_not_inside_it(self):
        # A worker whose cwd is $DIR/wt could edit ../verify-cmds.txt and
        # ../scope.txt, then pass its own verification.
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            stub = te.root / "usage-stub.py"
            write_usage_stub(
                stub,
                {
                    "primary_worker": "codex",
                    "fallback_workers": [],
                    "ranked": [{"cli": "codex", "score": 80}],
                    "worker_args": {"codex": []},
                    "reasons": [],
                },
            )
            env = dict(te.env)
            env["SSA_USAGE_PY"] = str(stub)
            env["SSA_STUB_ARGV"] = str(te.root / "usage-argv.txt")

            rc, out, err = run_ssa("init", "--repo", str(repo), env=env)
            self.assertEqual(rc, 0, err)
            task_dir = find_only_task_dir(te.work_dir)
            wt = Path((task_dir / "wt.txt").read_text().strip())
            self.assertTrue(wt.is_dir())
            self.assertFalse(str(wt).startswith(str(task_dir) + os.sep))
            self.assertEqual(wt.parent, te.work_dir / "wt")
            self.assertEqual(wt.name, task_dir.name)

    def test_the_work_dir_ownership_check_is_wired(self):
        # Not portably testable as a behavior (it needs a dir owned by another
        # user), so the guard itself is the assertion: `chmod 700` is a silent
        # no-op on a directory somebody else owns.
        sh = SSA_SH.read_text()
        self.assertIn('[[ -O "$SSA_WORK_DIR" ]] ||', sh)
        self.assertIn("_ssa_ensure_work_dir", sh)


class SecretScanTests(unittest.TestCase):
    HEX40 = "9f2c1a7e4b8d0356af91c2e7d40b6813a5c9e2f7"

    def test_an_untracked_file_holding_a_hex_token_trips_the_scan(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (repo / "leaked.txt").write_text("API_TOKEN=%s\n" % self.HEX40)

            rc, out, err = run_ssa("scan-secrets", "--dir", str(task_dir), env=te.env)
            self.assertNotEqual(rc, 0, "untracked credential passed the scan")
            findings = (task_dir / "verify-secrets.txt").read_text()
            self.assertIn("high-entropy-token", findings)
            self.assertNotIn(self.HEX40, findings)

    def test_verify_records_whether_gitleaks_ran(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            rc, out, err = run_ssa("verify", "--dir", str(task_dir), env=te.env)
            self.assertEqual(rc, 0, err)
            doc = json.loads((task_dir / "outcome.json").read_text())["verify"]
            self.assertIn(doc["gitleaks"], ("ran", "absent"))


class DoctorTests(unittest.TestCase):
    def test_doctor_never_prints_an_expanded_home_path(self):
        with temp_env() as te:
            env = dict(te.env)
            env["CODEX_BIN"] = str(te.root / "no-such-codex")
            rc, out, err = run_ssa("doctor", env=env)
            auth_rows = [l for l in out.splitlines() if "auth:" in l and "at " in l]
            self.assertTrue(auth_rows, out)
            for row in auth_rows:
                self.assertNotIn(str(te.home), row)
                self.assertIn("~/", row)

    def test_doctor_reports_env_scrub_from_the_registry(self):
        with temp_env() as te:
            rc, out, err = run_ssa("doctor", env=te.env)
            rows = [l for l in out.splitlines() if "env-scrub" in l]
            self.assertEqual(len(rows), 1, out)
            self.assertIn("scrubbed:", rows[0])


class ResumeTests(unittest.TestCase):
    def test_resume_dispatch_passes_the_recorded_session_id(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "session-id.txt").write_text("sess-abc-123\n")
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex",
                "--resume", env=env,
            )
            self.assertEqual(rc, 0, err)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-codex" / "argv.txt")
            self.assertEqual(argv[:3], ["exec", "resume", "sess-abc-123"])

    def test_background_resume_keeps_the_session_id_it_resumes(self):
        # A re-dispatch clears the previous run's artifacts, and session-id.txt
        # is the one input a resume cannot lose.
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "session-id.txt").write_text("sess-bg-9\n")
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex",
                "--resume", "--background", env=env,
            )
            self.assertEqual(rc, 0, err + out)
            deadline = time.time() + 30
            while time.time() < deadline:
                if (task_dir / "exit-code.txt").exists():
                    break
                time.sleep(0.2)
            else:
                self.fail("background resume never finished\n"
                          + (task_dir / "bg.log").read_text())
            wait_for_background_dispatch(task_dir)
            argv = read_argv_file(te.home / ".ssa-test" / "fake-codex" / "argv.txt")
            self.assertEqual(argv[:3], ["exec", "resume", "sess-bg-9"])

    def test_resume_refuses_without_a_session_id(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "resume-unavailable.txt").write_text("none\n")
            env = dict(te.env)
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "codex",
                "--resume", env=env,
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("resume", err)


class SteerTests(unittest.TestCase):
    def _wait_for(self, path, seconds=40):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if path.exists():
                return True
            time.sleep(0.2)
        return False

    def _env_with_ps_shim(self, te, extra=None):
        """A PATH-front ps that answers lstart/pgid without calling /bin/ps.

        _worker_state treats an unreadable start time as reused, so a live
        sleeper would refuse steer in environments where ps is blocked.
        """
        bindir = te.root / "bin"
        bindir.mkdir(exist_ok=True)
        shim = bindir / "ps"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "args, fmt, pid = sys.argv[1:], None, None\n"
            "i = 0\n"
            "while i < len(args):\n"
            "    a = args[i]\n"
            "    if a == '-o' and i + 1 < len(args):\n"
            "        fmt = args[i + 1]; i += 2; continue\n"
            "    if a.startswith('-o'):\n"
            "        fmt = a[2:]; i += 1; continue\n"
            "    if a == '-p' and i + 1 < len(args):\n"
            "        pid = args[i + 1]; i += 2; continue\n"
            "    if a.startswith('-p'):\n"
            "        pid = a[2:]; i += 1; continue\n"
            "    i += 1\n"
            "if not pid:\n"
            "    sys.exit(1)\n"
            "try:\n"
            "    pid_i = int(pid)\n"
            "    os.kill(pid_i, 0)\n"
            "except (OSError, ValueError):\n"
            "    sys.exit(1)\n"
            "fmt = (fmt or '').rstrip('=')\n"
            "if fmt == 'lstart':\n"
            "    print('Thu Jan  1 00:00:00 2026')\n"
            "elif fmt == 'pgid':\n"
            "    try:\n"
            "        print(os.getpgid(pid_i))\n"
            "    except OSError:\n"
            "        sys.exit(1)\n"
            "else:\n"
            "    sys.exit(1)\n"
        )
        shim.chmod(0o755)
        env = dict(te.env)
        env["PATH"] = "%s:%s" % (bindir, env.get("PATH", ""))
        if extra:
            env.update(extra)
        return env

    def test_no_pid(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            rc, out, err = run_ssa(
                "steer", "--dir", str(task_dir), "--message", "hello", env=te.env
            )
            self.assertEqual(rc, 1, err + out)
            self.assertFalse((task_dir / "steer.txt").exists())

    def test_not_running(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            dead = subprocess.Popen(["true"])
            dead.wait()
            (task_dir / "worker.pid").write_text("%d\n" % dead.pid)
            rc, out, err = run_ssa(
                "steer", "--dir", str(task_dir), "--message", "hello", env=te.env
            )
            self.assertEqual(rc, 1, err + out)
            self.assertFalse((task_dir / "steer.txt").exists())

    def test_reused(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (task_dir / "worker.pid").write_text("%d\n" % os.getpid())
            (task_dir / "worker-start.txt").write_text("Thu Jan  1 00:00:00 1970\n")
            rc, out, err = run_ssa(
                "steer", "--dir", str(task_dir), "--message", "hello", env=te.env
            )
            self.assertEqual(rc, 1, err + out)
            self.assertFalse((task_dir / "steer.txt").exists())

    def test_delivers(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            sleeper = write_script(te.root / "codex-slow.sh", "#!/bin/sh\nsleep 30\n")
            env = self._env_with_ps_shim(
                te,
                extra={
                    "CODEX_BIN": str(sleeper),
                    "SSA_KILL_GRACE_SECS": "2",
                },
            )
            task_id = (task_dir / "task-id.txt").read_text()
            proc = subprocess.Popen(
                ["bash", str(SSA_SH), "dispatch", "--dir", str(task_dir),
                 "--worker", "codex"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                self.assertTrue(self._wait_for(task_dir / "worker.pid", 20))
                seen = ""
                deadline = time.time() + 20
                while time.time() < deadline:
                    _, seen, _ = run_ssa("status", "--dir", str(task_dir), env=env)
                    if "(running)" in seen:
                        break
                    time.sleep(0.2)
                self.assertIn("(running)", seen)
                rc, out, err = run_ssa(
                    "steer", "--dir", str(task_dir), "--message", "course-correct",
                    env=env,
                )
                self.assertEqual(rc, 0, err + out)
                self.assertEqual((task_dir / "steer.txt").read_text(), "course-correct\n")
                _, seen, _ = run_ssa("status", "--dir", str(task_dir), env=env)
                self.assertIn("(running)", seen)
                self.assertEqual((task_dir / "task-id.txt").read_text(), task_id)
                self.assertFalse((task_dir / "exit-code.txt").exists())
                self.assertFalse((task_dir / "stopped.txt").exists())
            finally:
                run_ssa("stop", "--dir", str(task_dir), env=env)
                proc.wait(timeout=60)


class VersionTests(unittest.TestCase):
    def test_plugin_version_matches_the_top_changelog_heading(self):
        import re

        version = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
        headings = re.findall(
            r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(), re.M
        )
        self.assertTrue(headings, "CHANGELOG.md has no version heading")
        self.assertEqual(version, headings[0])

if __name__ == "__main__":
    unittest.main()
