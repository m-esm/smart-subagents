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
    make_git_repo,
    read_argv_file,
    run_ssa,
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


def find_only_task_dir(work_dir: Path) -> Path:
    dirs = [p for p in work_dir.iterdir() if p.is_dir()]
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
                *brief_text.rstrip("\n").split("\n"),
                "--cwd",
                str(repo),
                "--sandbox",
                "workspace",
                "--reasoning-effort",
                "high",
                "--output-format",
                "streaming-messages-json",
                "--include-partial-messages",
            ]
            self.assertEqual(argv, expected)
            self.assertEqual((task_dir / "exit-code.txt").read_text().strip(), "0")
            self.assertTrue((task_dir / "session-id.txt").read_text().strip())

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


if __name__ == "__main__":
    unittest.main()
