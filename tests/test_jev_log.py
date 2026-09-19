"""Hermetic regressions for durable advisory decisions and outcome joins."""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import make_git_repo, run_ssa, run_ssa_cli_stdin, temp_env
import test_jev
from test_jev import FakeJev, GOOD_BRIEF, FLIPPED, all_yes, jev_env
from test_shell import write_usage_stub
from ssa import jev_log

SENTINEL = "PRIVATE-CONTENT-8d82e9b4"
CLASS = {
    "size": {"probabilities": {"0": 0.1, "1": 0.9}, "confidence": 0.9},
    "difficulty": {"probabilities": {"1": 0.28, "2": 0.72}, "confidence": 0.72},
    "kind": {"choice": "debug", "confidence": 0.9},
}


class DecisionLog(unittest.TestCase):
    def test_cli_schema_privacy_manual_and_write_failure(self):
        fake = FakeJev({**CLASS, **all_yes(), "inverts_assertion": {"noul": 0.9}})
        self.addCleanup(fake.close)
        with temp_env() as te:
            env = jev_env(te, fake, SSA_JEV_MODEL="jev-pinned")
            report = te.root / "report.txt"
            report.write_text(SENTINEL)
            path = Path(env["SSA_JEV_DECISIONS"])
            for stage, content, extra in (
                ("classify", GOOD_BRIEF + SENTINEL, []),
                ("lint", SENTINEL, []),
                ("review", FLIPPED.replace("spare", SENTINEL), ["--report", str(report)]),
            ):
                with self.subTest(stage=stage):
                    manual = run_ssa_cli_stdin(content, "jev", stage, *extra, env=env)
                    before = path.read_text() if path.exists() else ""
                    result = run_ssa_cli_stdin(content, "jev", stage, *extra,
                                              "--task-id", "task-1", "--action", "advisory", env=env)
                    self.assertEqual(result, manual)
                    lines = path.read_text().splitlines()
                    self.assertEqual(len(lines), len(before.splitlines()) + 1)
                    row = json.loads(lines[-1])
                    self.assertEqual(set(row), {"schema_version", "ts", "task_id", "stage", "model", "action"}
                                     | set(jev_log.FIELDS[stage]))
                    self.assertEqual(row["model"], "jev-pinned")
                    self.assertEqual(row["stage"], stage)
                    self.assertEqual(row["task_id"], "task-1")
                    self.assertEqual(row["schema_version"], 1)
                    self.assertIsNotNone(jev_log.timestamp(row))
                    for key in jev_log.FIELDS[stage]:
                        self.assertEqual(row[key], json.loads(result[1])[key])
                    broken = dict(env, SSA_JEV_DECISIONS=str(te.root))
                    self.assertEqual(run_ssa_cli_stdin(content, "jev", stage, *extra,
                                     "--task-id", "task-1", env=broken), manual)
            self.assertNotIn(SENTINEL, path.read_text())

    def test_unavailable_and_skipped_reviews_do_not_log(self):
        with temp_env() as te:
            env = dict(te.env, SSA_JEV="1", TYPESAFE_API_KEY="test-key",
                       SSA_JEV_URL="http://127.0.0.1:1", SSA_JEV_DECISIONS=str(te.root / "log"))
            for stage, text in (("classify", GOOD_BRIEF), ("lint", GOOD_BRIEF), ("review", FLIPPED)):
                for disabled in (False, True):
                    current = dict(env, SSA_JEV="0" if disabled else "1")
                    manual = run_ssa_cli_stdin(text, "jev", stage, env=current)
                    logged = run_ssa_cli_stdin(text, "jev", stage, "--task-id", "task", env=current)
                    self.assertEqual(logged, manual)
                    self.assertEqual(logged[0], 2)
            result = run_ssa_cli_stdin("no test hunks", "jev", "review", "--task-id", "task", env=env)
            self.assertEqual(result[0], 0)
            self.assertFalse(Path(env["SSA_JEV_DECISIONS"]).exists())

    def test_shell_lint_review_each_append_once(self):
        fake = FakeJev({"inverts_assertion": {"noul": 0.91}})
        self.addCleanup(fake.close)
        with temp_env() as te:
            env = jev_env(te, fake)
            task = test_jev.VerifyReview().make_task(te)
            (task / "task-id.txt").write_text("task-1\n")
            (task / "brief.md").write_text(GOOD_BRIEF + SENTINEL)
            (task / "last-msg.txt").write_text(SENTINEL)
            repo = Path((task / "wt.txt").read_text().strip())
            test_file = repo / "tests/test_scene.py"
            test_file.write_text(test_file.read_text() + "# " + SENTINEL + "\n")
            for action in ("preflight", "review"):
                rc, _, err = run_ssa("jev", action, "--dir", str(task), env=env)
                self.assertEqual(rc, 0, err)
            text = Path(env["SSA_JEV_DECISIONS"]).read_text()
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertEqual([row["stage"] for row in rows], ["lint", "review"])
            self.assertEqual([row["action"] for row in rows], ["advisory", "advisory"])
            self.assertNotIn(SENTINEL, text)

    def test_path_resolution(self):
        with patch.dict(os.environ, {"HOME": "/tmp/jev-home"}, clear=True):
            self.assertEqual(str(jev_log.state_path("SSA_LEDGER", "outcomes.jsonl")),
                             "/tmp/jev-home/.local/state/smart-subagents/outcomes.jsonl")
            os.environ["XDG_STATE_HOME"] = "/tmp/jev-state"
            self.assertEqual(str(jev_log.state_path("SSA_JEV_DECISIONS", "jev-decisions.jsonl")),
                             "/tmp/jev-state/smart-subagents/jev-decisions.jsonl")


class InitBrief(unittest.TestCase):
    def test_effective_class_explicit_axes_and_unavailable(self):
        fake = FakeJev(CLASS)
        self.addCleanup(fake.close)
        for flags, mode, expected, action in (
            ([], "on", ["small", "routine", "debug"], "applied"),
            (["--difficulty", "frontier"], "on", ["small", "frontier", "debug"], "partial-explicit-flags"),
            (["--difficulty", "hard", "--size", "large", "--kind", "review"], "on",
             ["large", "hard", "review"], "skipped-explicit-flags"),
            ([], "off", ["medium", "routine", "default"], None),
            ([], "dead", ["medium", "routine", "default"], None),
        ):
            with self.subTest(mode=mode, flags=flags), temp_env() as te:
                repo = make_git_repo(te.root / "repo")
                stub = te.root / "usage.py"
                write_usage_stub(stub, {"primary_worker": "codex", "ranked": [{"cli": "codex", "score": 80}]})
                env = jev_env(te, fake, SSA_USAGE_PY=str(stub), SSA_STUB_ARGV=str(te.root / "argv"))
                if mode == "off":
                    env["SSA_JEV"] = "0"
                if mode == "dead":
                    env["SSA_JEV_URL"] = "http://127.0.0.1:1"
                brief = te.root / "brief.md"
                brief.write_text(GOOD_BRIEF + SENTINEL)
                rc, out, err = run_ssa("init", "--repo", str(repo), "--brief", str(brief), *flags, env=env)
                self.assertEqual(rc, 0, out + err)
                task = Path(json.loads(out)["dir"])
                self.assertEqual([(task / (axis + ".txt")).read_text().strip()
                                  for axis in ("size", "difficulty", "kind")], expected)
                self.assertEqual((task / "classify.json").exists(), mode == "on")
                log = Path(env["SSA_JEV_DECISIONS"])
                if action:
                    rows = log.read_text().splitlines()
                    self.assertEqual(len(rows), 1)
                    row = json.loads(rows[0])
                    self.assertEqual(row["action"], action)
                    self.assertEqual(row["task_id"], json.loads(out)["task_id"])
                    self.assertEqual(row["difficulty"], "hard")
                    self.assertEqual(row["effective"]["difficulty"], "routine")
                    self.assertNotIn(SENTINEL, rows[0])
                else:
                    self.assertFalse(log.exists())


class LedgerJev(unittest.TestCase):
    def test_optional_fields_and_invalid_sources(self):
        with temp_env() as te:
            env = dict(te.env, SSA_LEDGER=str(te.root / "outcomes.jsonl"))
            task = te.root / "task"
            task.mkdir()
            def record():
                rc, out, err = run_ssa("record", "--dir", str(task), "--outcome", "partial", env=env)
                self.assertEqual(rc, 0, out + err)
                return json.loads(Path(env["SSA_LEDGER"]).read_text().splitlines()[-1])
            row = record()
            for key in ("jev", "parent_task", "slice"):
                self.assertNotIn(key, row)
            classify = {"size": "small", "difficulty": "hard", "kind": "debug",
                        "effective": {"difficulty": "routine"}, "confidence": {"difficulty": 0.72},
                        "runner_up": {"difficulty": "routine"}}
            (task / "classify.json").write_text(json.dumps(dict(classify, flags=SENTINEL)))
            (task / "brief-lint.json").write_text(json.dumps({"missing": ["goal"], "text": SENTINEL}))
            (task / "diff-review.json").write_text(json.dumps({"flags": ["disables_test"], "files": [SENTINEL]}))
            (task / "parent-task.txt").write_text(" parent-1\n")
            (task / "slice.txt").write_text(" 2 \n")
            row = record()
            self.assertEqual(row["jev"], {"classify": classify, "lint_missing": ["goal"],
                                          "review_flags": ["disables_test"]})
            self.assertEqual(row["schema_version"], 1)
            self.assertEqual((row["parent_task"], row["slice"]), ("parent-1", "2"))
            self.assertNotIn(SENTINEL, json.dumps(row))
            (task / "classify.json").write_text("{")
            (task / "brief-lint.json").write_text("null")
            self.assertEqual(record()["jev"], {"review_flags": ["disables_test"]})
            (task / "diff-review.json").write_text("[]")
            self.assertNotIn("jev", record())


class Tune(unittest.TestCase):
    def test_fixture_aggregates_latest_window_and_table(self):
        with temp_env() as te:
            env = dict(te.env, SSA_LEDGER=str(te.root / "outcomes.jsonl"),
                       SSA_JEV_DECISIONS=str(te.root / "decisions.jsonl"))
            now = datetime.now(timezone.utc)
            rows = []
            for stage in ("classify", "lint", "review"):
                for task in ("pass", "partial", "unjoined", "old"):
                    rows.append({"ts": (now - timedelta(days=40 if task == "old" else 1)).isoformat(),
                                 "task_id": task, "stage": stage,
                                 "effective": {"difficulty": "routine" if task == "pass" else "hard"},
                                 "low_confidence": ["difficulty"] if task == "pass" else [],
                                 "scores": {"goal": 0.5, "verify": 0.6},
                                 "missing": ["goal"] if task == "partial" else [],
                                 "flags": ["disables_test"] if task == "partial" else []})
            # An older duplicate comes last to catch joins that trust file order.
            rows.append(dict(rows[0], ts=(now - timedelta(days=2)).isoformat(),
                             effective={"difficulty": "frontier"}))
            Path(env["SSA_JEV_DECISIONS"]).write_text("\n".join(map(json.dumps, rows)) + "\n{broken\n")
            outcomes = [{"task_id": "pass", "outcome": "partial", "verification_passed": False},
                        {"task_id": "pass", "outcome": "verified-pass", "verification_passed": True},
                        {"task_id": "partial", "outcome": "partial", "verification_passed": False}]
            Path(env["SSA_LEDGER"]).write_text("\n".join(map(json.dumps, outcomes)))
            rc, out, err = run_ssa("jev", "tune", "--json", env=env)
            self.assertEqual(rc, 0, err)
            report = json.loads(out)
            self.assertEqual(report["days"], 30)
            stages = report["stages"]
            for stats in stages.values():
                self.assertEqual((stats["decisions"], stats["joined_outcomes"]), (3, 2))
            cls = stages["classify"]
            self.assertNotIn("frontier", cls["effective_difficulty"])
            self.assertEqual(cls["effective_difficulty"]["routine"]["verified_pass_rate"], 1)
            self.assertEqual(cls["low_confidence"]["false"]["verified_pass_rate"], 0)
            self.assertEqual(cls["low_confidence"]["true"]["verified_pass_rate"], 1)
            self.assertEqual(stages["lint"]["elements"]["goal"]["missing"]["verified_pass_rate"], 0)
            self.assertEqual(stages["lint"]["elements"]["goal"]["not_missing"]["verified_pass_rate"], 1)
            self.assertEqual(stages["review"]["flagged"]["true"]["partial"], 1)
            self.assertEqual(stages["review"]["flagged"]["false"]["verified_pass"], 1)
            rc, out, err = run_ssa("jev", "tune", env=env)
            self.assertEqual(rc, 0, err)
            self.assertIn("classify 3 2", out)
            self.assertIn("lint/elements/goal/not_missing 1 1 0 100.0%", out)
            self.assertIn("review/flagged/true 1 0 1 0.0%", out)
            self.assertNotIn("unjoined", out)
            rc, out, err = run_ssa("jev", "tune", "--days", "60", "--json", env=env)
            self.assertEqual(json.loads(out)["stages"]["classify"]["decisions"], 4)

    def test_missing_empty_files_and_invalid_days(self):
        with temp_env() as te:
            env = dict(te.env, SSA_LEDGER=str(te.root / "outcomes.jsonl"),
                       SSA_JEV_DECISIONS=str(te.root / "decisions.jsonl"))
            for exists in (False, True):
                if exists:
                    Path(env["SSA_JEV_DECISIONS"]).touch()
                rc, out, err = run_ssa("jev", "tune", env=env)
                self.assertEqual((rc, out.strip()), (0, "no decisions"), err)
                rc, out, err = run_ssa("jev", "tune", "--json", env=env)
                self.assertEqual(rc, 0, err)
                self.assertTrue(all(s["decisions"] == 0 for s in json.loads(out)["stages"].values()))
            rc, _, _ = run_ssa("jev", "tune", "--days", "-1", env=env)
            self.assertNotEqual(rc, 0)
