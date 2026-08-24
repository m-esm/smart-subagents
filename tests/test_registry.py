"""Conformance test for the claim that a fourth worker is a registry entry.

Every test in FourthWorkerTests registers a worker called "fakecli" through
SSA_WORKERS_JSON and nothing else: no Python is edited, no case arm is added,
no name is taught to scripts/. If any of these start failing because someone
had to special-case a worker in code, that is the regression this file exists
to catch.

The rest of the file pins the guarantees the seam rests on: templates that
could reach a shell are refused, illegal lifecycle transitions raise, event
sequence numbers are monotonic, and an interrupted write cannot destroy the
previous task record.

Hermetic: the fourth worker is tests/fixtures/bin/fake-fakecli, which records
its argv instead of doing anything.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (  # noqa: E402
    BIN_DIR,
    FIXTURE_BRIEF,
    WORKERS_JSON,
    load_ssa,
    make_git_repo,
    read_argv_file,
    run_ssa,
    run_ssa_cli,
    temp_env,
)
from test_shell import find_only_task_dir, make_task_dir, write_usage_stub  # noqa: E402

FAKE_ENTRY = {
    "display_name": "Fake CLI (test fixture)",
    "binary": {"env": "FAKECLI_BIN", "candidates": [], "path_name": "fake-fakecli"},
    "auth_file": "~/.fakecli/auth.json",
    "probe": "check_fakecli",
    "sandbox": "workspace",
    "write_allowed_default": True,
    "run": {"cwd": "inherit", "env_scrub": False},
    "prompt": {
        "implement": {"transport": "arg"},
        "plan": {"transport": "arg"},
        "resume": {"transport": "arg"},
    },
    "argv": {
        "implement": ["run", "--dir", "{worktree}", "--prompt", "{prompt}", "{effort}"],
        "plan": ["plan", "--dir", "{worktree}", "--prompt", "{prompt}", "{effort}"],
        "resume": ["run", "--session", "{session_id}", "--prompt", "{prompt}"],
    },
    "output": {"implement": "none", "plan": "stdout", "resume": "none"},
    "format": {"implement": "json", "plan": "text", "resume": "json"},
    "session": {"kind": "json-keys", "keys": ["session_id"]},
    "effort_ladder": ["low", "high"],
    "effort_flags": ["--think", "{effort}"],
    "models": {
        "flag": ["--model", "{model}"],
        "rules": [{"difficulty": ["trivial"], "model": "fake-fast"}],
    },
    "fit": {"default": 1.0, "impl": 1.1},
}


def write_registry(path: Path, with_fake: bool = False, mutate=None) -> Path:
    """The shipped registry plus (optionally) a fourth worker.

    Derived from scripts/workers.json rather than copied, so this fixture
    cannot drift away from the real thing.
    """
    doc = json.loads(WORKERS_JSON.read_text())
    if with_fake:
        doc["workers"]["fakecli"] = json.loads(json.dumps(FAKE_ENTRY))
    if mutate:
        mutate(doc)
    path.write_text(json.dumps(doc, indent=2))
    return path


def registry_env(te, **extra) -> dict:
    env = dict(te.env)
    env["SSA_WORKERS_JSON"] = str(
        write_registry(te.root / "workers.json", with_fake=True)
    )
    env["FAKECLI_BIN"] = str(BIN_DIR / "fake-fakecli")
    env.update(extra)
    return env


class FourthWorkerTests(unittest.TestCase):
    def test_registry_validates_and_lists_the_fourth_worker(self):
        with temp_env() as te:
            env = registry_env(te)
            rc, out, err = run_ssa_cli("registry-validate", env=env)
            self.assertEqual(rc, 0, err)
            self.assertIn("fakecli", out)

            rc, out, err = run_ssa_cli("workers", env=env)
            self.assertEqual(rc, 0, err)
            names = [line.split("\t")[0] for line in out.strip().splitlines()]
            self.assertEqual(names, ["codex", "grok", "kimi", "claude", "fakecli"])
            row = dict(
                zip(
                    ("name", "display", "sandbox", "write", "probe", "bin", "auth"),
                    out.strip().splitlines()[-1].split("\t"),
                )
            )
            self.assertEqual(row["sandbox"], "workspace")
            self.assertEqual(row["probe"], "check_fakecli")
            self.assertEqual(row["bin"], str(BIN_DIR / "fake-fakecli"))

    def test_unknown_probe_is_unavailable_not_invented_headroom(self):
        with temp_env() as te:
            env = registry_env(te)
            proc = subprocess.run(
                [sys.executable, "scripts/ai-cli-usage.py", "--json", "--cli", "fakecli"],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            # Exit 2 means "nothing available", which is the right answer here.
            self.assertIn(proc.returncode, (0, 2), proc.stderr)
            doc = json.loads(proc.stdout)
            entry = next(c for c in doc["clis"] if c["cli"] == "fakecli")
            self.assertFalse(entry["available"])
            self.assertFalse(entry["eligible"])
            self.assertEqual(entry["skip_reason"], "no quota probe")
            self.assertEqual(entry["score"], 0.0)
            self.assertEqual(doc["recommendation"]["primary_worker"], None)

    def test_init_picks_the_fourth_worker_when_routing_ranks_it_first(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            stub = te.root / "usage-stub.py"
            write_usage_stub(
                stub,
                {
                    "primary_worker": "fakecli",
                    "fallback_workers": ["codex"],
                    "local_labor_ok": True,
                    "worker_args": {"fakecli": ["--think", "high"], "codex": []},
                    "ranked": [
                        {"cli": "fakecli", "score": 90},
                        {"cli": "codex", "score": 40},
                    ],
                    "reasons": [],
                },
            )
            env = registry_env(te)
            env["SSA_USAGE_PY"] = str(stub)
            env["SSA_STUB_ARGV"] = str(te.root / "usage-argv.txt")

            rc, out, err = run_ssa("init", "--repo", str(repo), env=env)
            self.assertEqual(rc, 0, err)
            task_dir = find_only_task_dir(te.work_dir)
            self.assertEqual((task_dir / "worker.txt").read_text().strip(), "fakecli")
            self.assertEqual(
                (task_dir / "worker-args.txt").read_text().strip(), "--think\nhigh"
            )
            self.assertEqual(json.loads(out)["worker"], "fakecli")

    def test_dispatch_runs_the_fourth_worker_with_the_templated_argv(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            brief_text = FIXTURE_BRIEF
            task_dir = make_task_dir(
                te.work_dir, repo, worker_args=["--think", "high"], brief_text=brief_text
            )
            env = registry_env(te)

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "fakecli", env=env
            )
            self.assertEqual(rc, 0, err)

            recorder = te.home / ".ssa-test" / "fake-fakecli"
            argv = read_argv_file(recorder / "argv.txt")
            self.assertEqual(
                argv,
                [
                    "run",
                    "--dir",
                    str(repo),
                    "--prompt",
                    *brief_text.rstrip("\n").split("\n"),
                    "--think",
                    "high",
                ],
            )
            self.assertEqual((task_dir / "exit-code.txt").read_text().strip(), "0")
            self.assertEqual(
                (task_dir / "session-id.txt").read_text().strip(),
                "fake-fakecli-session-000000000001",
            )
            self.assertFalse((task_dir / "resume-unavailable.txt").exists())

    def test_dispatch_captures_a_failing_exit_code_from_the_fourth_worker(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (te.home / ".ssa-test-control.json").write_text(
                json.dumps({"exit_code": 3, "session_id": ""})
            )
            env = registry_env(te)

            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "fakecli", env=env
            )
            self.assertEqual(rc, 3, err)
            self.assertEqual((task_dir / "exit-code.txt").read_text().strip(), "3")
            self.assertTrue((task_dir / "resume-unavailable.txt").exists())
            doc = json.loads((task_dir / "task.json").read_text())
            self.assertEqual(doc["state"], "exited")
            self.assertEqual(doc["attempts"][-1]["exit"], 3)
            self.assertEqual(doc["attempts"][-1]["failure_class"], "unknown")

    def test_plan_round_robins_onto_the_fourth_worker(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            stub = te.root / "usage-stub.py"
            write_usage_stub(
                stub,
                {
                    "primary_worker": "codex",
                    "fallback_workers": ["fakecli"],
                    "ranked": [
                        {"cli": "codex", "score": 80},
                        {"cli": "fakecli", "score": 70},
                    ],
                    "worker_args": {
                        "codex": ["-c", "model_reasoning_effort=high"],
                        "fakecli": ["--think", "high"],
                    },
                    "reasons": [],
                },
            )
            env = registry_env(te)
            env["SSA_USAGE_PY"] = str(stub)
            env["SSA_STUB_ARGV"] = str(te.root / "usage-argv.txt")
            env["CODEX_BIN"] = str(BIN_DIR / "fake-codex")

            rc, out, err = run_ssa(
                "plan",
                "--repo",
                str(repo),
                "--n",
                "2",
                "--goal",
                "Plan the fixture.",
                env=env,
            )
            self.assertEqual(rc, 0, err)
            doc = json.loads(out)
            self.assertEqual(doc["planners"], ["pragmatic:codex", "risk:fakecli"])
            for plan in doc["plans"]:
                self.assertFalse(plan["empty"], plan)

            argv = read_argv_file(te.home / ".ssa-test" / "fake-fakecli" / "argv.txt")
            self.assertEqual(argv[0], "plan")
            self.assertIn("--think", argv)
            self.assertIn("high", argv)

    def test_record_writes_a_ledger_line_naming_the_fourth_worker(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo)
            (task_dir / "worker.txt").write_text("fakecli\n")
            (task_dir / "exit-code.txt").write_text("0\n")
            env = registry_env(te)

            rc, out, err = run_ssa(
                "record",
                "--dir",
                str(task_dir),
                "--outcome",
                "verified-pass",
                env=env,
            )
            self.assertEqual(rc, 0, err)
            record = json.loads(
                (te.state_dir / "outcomes.jsonl").read_text().strip().splitlines()[-1]
            )
            self.assertEqual(record["worker"], "fakecli")
            self.assertEqual(record["outcome"], "verified-pass")


class RegistryValidationTests(unittest.TestCase):
    def _validate(self, te, mutate) -> tuple:
        path = write_registry(te.root / "bad.json", with_fake=True, mutate=mutate)
        env = dict(te.env)
        env["SSA_WORKERS_JSON"] = str(path)
        return run_ssa_cli("registry-validate", env=env)

    def test_unknown_placeholder_is_rejected(self):
        with temp_env() as te:
            rc, out, err = self._validate(
                te,
                lambda doc: doc["workers"]["fakecli"]["argv"].__setitem__(
                    "implement", ["run", "--dir", "{banana}", "--prompt", "{prompt}"]
                ),
            )
            self.assertEqual(rc, 1)
            self.assertIn("unknown placeholder {banana}", err)

    def test_shell_metacharacter_in_a_template_is_rejected(self):
        with temp_env() as te:
            for token in ("run; rm -rf /", "run && curl x", "$(whoami)", "a|b", "x`id`"):
                with self.subTest(token=token):
                    rc, out, err = self._validate(
                        te,
                        lambda doc, tok=token: doc["workers"]["fakecli"][
                            "argv"
                        ].__setitem__("implement", [tok, "--prompt", "{prompt}"]),
                    )
                    self.assertEqual(rc, 1, out)
                    self.assertIn("fakecli", err)

    def test_missing_required_field_is_rejected(self):
        with temp_env() as te:
            rc, out, err = self._validate(
                te, lambda doc: doc["workers"]["fakecli"].pop("probe")
            )
            self.assertEqual(rc, 1)
            self.assertIn("missing required field 'probe'", err)

    def test_wrong_schema_version_is_rejected(self):
        with temp_env() as te:
            rc, out, err = self._validate(te, lambda doc: doc.update({"schema_version": 99}))
            self.assertEqual(rc, 1)
            self.assertIn("schema_version", err)

    def test_stdin_transport_may_not_also_pass_the_prompt_as_an_argument(self):
        with temp_env() as te:
            def mutate(doc):
                doc["workers"]["fakecli"]["prompt"]["implement"] = {"transport": "stdin"}
            rc, out, err = self._validate(te, mutate)
            self.assertEqual(rc, 1)
            self.assertIn("transport is stdin", err)


class StateMachineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = load_ssa("state")

    def _dir(self, te, name="task") -> Path:
        d = te.root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "task-id.txt").write_text("t-1\n")
        return d

    def test_legal_path_walks_the_whole_lifecycle(self):
        with temp_env() as te:
            d = self._dir(te)
            for step in (
                "minted",
                "preflighted",
                "picked",
                "running",
                "exited",
                "verified",
                "reported",
            ):
                doc = self.state.transition(str(d), step)
                self.assertEqual(doc["state"], step)
            self.assertEqual(len(doc["attempts"]), 1)

    def test_illegal_transition_raises(self):
        with temp_env() as te:
            d = self._dir(te)
            self.state.transition(str(d), "minted")
            with self.assertRaises(self.state.StateError) as ctx:
                self.state.transition(str(d), "running")
            self.assertIn("illegal transition minted -> running", str(ctx.exception))
            # The refusal must not have moved the record.
            self.assertEqual(self.state.load(str(d))["state"], "minted")

    def test_terminal_states_refuse_to_restart_work(self):
        with temp_env() as te:
            d = self._dir(te)
            for step in ("minted", "preflighted", "picked", "running", "aborted"):
                self.state.transition(str(d), step)
            with self.assertRaises(self.state.StateError):
                self.state.transition(str(d), "running")

    def test_illegal_transition_through_the_cli_exits_nonzero(self):
        with temp_env() as te:
            d = self._dir(te)
            rc, out, err = run_ssa_cli(
                "transition", "--dir", str(d), "--to", "minted", env=te.env
            )
            self.assertEqual(rc, 0, err)
            rc, out, err = run_ssa_cli(
                "transition", "--dir", str(d), "--to", "reported", env=te.env
            )
            self.assertEqual(rc, 1)
            self.assertIn("illegal transition", err)

    def test_event_sequence_numbers_are_monotonic(self):
        with temp_env() as te:
            d = self._dir(te)
            for phase in ("minted", "preflighted", "picked", "running", "exited"):
                self.state.append_event(str(d), phase)
            events = self.state.read_events(str(d))
            self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4, 5])
            self.assertEqual(
                [e["phase"] for e in events],
                ["minted", "preflighted", "picked", "running", "exited"],
            )
            for event in events:
                self.assertTrue(event["ts"].endswith("Z"))

    def test_task_json_survives_an_interrupted_write(self):
        with temp_env() as te:
            d = self._dir(te)
            self.state.transition(str(d), "minted")
            self.state.transition(str(d), "preflighted")
            before = (d / "task.json").read_text()

            # 1. Serialization blows up: nothing is written at all.
            with self.assertRaises(TypeError):
                self.state._atomic_write_json(d / "task.json", {"bad": object()})
            self.assertEqual((d / "task.json").read_text(), before)

            # 2. The rename itself fails after the temp file is complete.
            real_replace = self.state.os.replace

            def boom(src, dst):
                raise OSError("simulated crash between write and rename")

            self.state.os.replace = boom
            try:
                with self.assertRaises(OSError):
                    self.state._atomic_write_json(d / "task.json", {"state": "clobbered"})
            finally:
                self.state.os.replace = real_replace

            self.assertEqual((d / "task.json").read_text(), before)
            leftovers = [p.name for p in d.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

            # The record is still usable afterwards.
            doc = self.state.transition(str(d), "picked")
            self.assertEqual(doc["state"], "picked")

    def test_state_falls_back_to_inference_without_a_task_json(self):
        with temp_env() as te:
            d = self._dir(te, name="legacy")
            (d / "wt.txt").write_text(str(te.root) + "\n")
            (d / "brief.md").write_text("brief\n")
            (d / "worker.txt").write_text("fakecli\n")
            rc, out, err = run_ssa_cli("state", "--dir", str(d), env=te.env)
            self.assertEqual(rc, 0, err)
            doc = json.loads(out)
            self.assertEqual(doc["state"], "picked")
            self.assertFalse(doc["recorded"])
            self.assertFalse((d / "task.json").exists())


class AdapterUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapters = load_ssa("adapters")
        cls.registry = load_ssa("registry")

    def _reg(self, te):
        path = write_registry(te.root / "workers.json", with_fake=True)
        return self.registry.load(str(path), cache=False)

    def test_effort_and_model_render_through_the_registry_templates(self):
        with temp_env() as te:
            reg = self._reg(te)
            brief = te.root / "brief.md"
            brief.write_text("do the thing\n")
            built = self.adapters.build_command(
                "fakecli",
                "implement",
                {
                    "worktree": str(te.root),
                    "brief": str(brief),
                    "effort": "high",
                    "model": "fake-fast",
                    "env": dict(te.env),
                },
                reg=reg,
            )
            self.assertEqual(
                built["argv"],
                [
                    "run",
                    "--dir",
                    str(te.root),
                    "--prompt",
                    "do the thing",
                    "--think",
                    "high",
                ],
            )
            self.assertIsNone(built["stdin"])
            self.assertFalse(built["env_scrub"])

    def test_effort_off_the_ladder_is_refused(self):
        with temp_env() as te:
            reg = self._reg(te)
            brief = te.root / "brief.md"
            brief.write_text("x\n")
            with self.assertRaises(self.adapters.AdapterError):
                self.adapters.build_command(
                    "fakecli",
                    "implement",
                    {"worktree": str(te.root), "brief": str(brief), "effort": "ludicrous"},
                    reg=reg,
                )

    def test_unknown_worker_is_refused(self):
        with temp_env() as te:
            reg = self._reg(te)
            with self.assertRaises(self.registry.RegistryError):
                self.adapters.build_command("nope", "implement", {}, reg=reg)

    def test_classify_failure_is_conservative(self):
        cases = [
            (0, "429 too many requests", None),
            (1, "HTTP 429 Too Many Requests", "rate-limit"),
            (1, "error: rate limit reached", "rate-limit"),
            (1, "401 Unauthorized", "auth"),
            (1, "please log in again", "auth"),
            (1, "segmentation fault", "unknown"),
            (1, "", "unknown"),
        ]
        for code, tail, expected in cases:
            with self.subTest(tail=tail):
                self.assertEqual(self.adapters.classify_failure(code, tail), expected)

    def test_codex_session_scrape_ignores_a_bare_id(self):
        with temp_env() as te:
            reg = self._reg(te)
            log = te.root / "codex.log"
            log.write_text(
                json.dumps({"type": "item.started", "id": "item_0123456789abcdef"})
                + "\n"
                + json.dumps({"type": "thread.started", "thread": "th_0123456789abcdef"})
                + "\n"
            )
            self.assertEqual(
                self.adapters.parse_session("codex", str(log), reg=reg),
                "th_0123456789abcdef",
            )

    def test_capabilities_come_from_the_registry(self):
        with temp_env() as te:
            reg = self._reg(te)
            self.assertEqual(
                self.adapters.capabilities("kimi", reg)["write_allowed_default"], False
            )
            self.assertEqual(self.adapters.capabilities("kimi", reg)["sandbox"], "none")
            self.assertTrue(self.adapters.capabilities("kimi", reg)["env_scrub"])
            self.assertTrue(
                self.adapters.capabilities("fakecli", reg)["write_allowed_default"]
            )


if __name__ == "__main__":
    unittest.main()
