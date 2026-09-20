"""Jev brief judgments: drift gate, answer mapping, and every fail-open path.

No test reaches TypeSafe. A local HTTP server plays the System One endpoint
(SSA_JEV_URL), answering from a table keyed by question id, and records what
it was sent so the request shape is asserted too.
"""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ROOT, load_usage_module, run_ssa, run_ssa_cli, temp_env  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from ssa import jev  # noqa: E402

GOOD_BRIEF = (
    "Workdir: /Users/dev/proj/audio. Implement a ring buffer in src/ring.go.\n"
    "Do not touch the public API.\nAcceptance: go test passes.\nVerify: go test ./...\n"
    "\n## Structural discovery\nCGC-SKIP: fixture; route=none; evidence=unit-test\n"
)

FLIPPED = """diff --git a/src/scene.py b/src/scene.py
--- a/src/scene.py
+++ b/src/scene.py
@@ -1,2 +1,2 @@
-NODES = ("scoop",)
+NODES = ("scoop", "spare")
diff --git a/tests/test_scene.py b/tests/test_scene.py
--- a/tests/test_scene.py
+++ b/tests/test_scene.py
@@ -4,3 +4,3 @@
     def test_spare_is_not_a_body(self):
-        self.assertNotIn("spare", NODES)
+        self.assertIn("spare", NODES)
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,2 @@
+def test_added():
+    assert True
"""


class FakeJev:
    """System One stand-in. `answers` maps question id -> answer fields."""

    def __init__(self, answers=None, status=200, fail_first=0):
        self.answers = answers or {}
        self.status = status
        self.fail_first = fail_first
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"body": body, "auth": self.headers.get("Authorization")})
                if outer.fail_first > 0:
                    outer.fail_first -= 1
                    self.send_response(429)
                    self.send_header("retry-after", "0.2")
                    self.end_headers()
                    return
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    return
                answers = {}
                for qid, q in body["questions"].items():
                    a = {"type": q["type"]}
                    a.update(outer.answers.get(qid, {}))
                    answers[qid] = a
                payload = json.dumps(
                    {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 7}}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1/systemone" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def jev_env(te, fake, **extra):
    env = dict(te.env)
    env.update({"SSA_JEV": "1", "SSA_JEV_URL": fake.url, "TYPESAFE_API_KEY": "test-key"})
    env.update({"SSA_JEV_DECISIONS": str(te.root / "decisions.jsonl"),
                "SSA_LEDGER": str(te.root / "outcomes.jsonl")})
    env.update(extra)
    return env


def all_yes():
    return {qid: {"noul": 0.02 if qid == "needs_answers" else 0.97} for qid in jev.LINT_QUESTIONS}


class DriftGate(unittest.TestCase):
    """Option keys are derived from the routing tables; only prose lives in jev.py."""

    def test_levels_match_the_routing_tables(self):
        usage = load_usage_module()
        self.assertEqual(list(jev.SIZE_LEVELS), list(usage.BASE_FLOOR))
        self.assertEqual(list(jev.DIFFICULTY_LEVELS), list(usage.DIFFICULTY))
        self.assertEqual(set(jev.KIND_OPTIONS), set(usage.FIT))

    def test_size_levels_are_ordered_by_quota_floor(self):
        usage = load_usage_module()
        floors = [usage.BASE_FLOOR[k] for k in jev.SIZE_LEVELS]
        self.assertEqual(floors, sorted(floors))


class Classify(unittest.TestCase):
    def test_maps_the_most_probable_level_to_its_key(self):
        fake = FakeJev(
            {
                "size": {"score": 1.4, "confidence": 0.9, "probabilities": {"0": 0.0, "1": 0.6, "2": 0.4, "3": 0.0}},
                "difficulty": {"score": 2.0, "confidence": 0.95, "probabilities": {"2": 1.0}},
                "kind": {"choice": "debug", "confidence": 0.8},
            }
        )
        try:
            with temp_env() as te:
                brief = te.root / "brief.md"
                brief.write_text(GOOD_BRIEF)
                rc, out, err = run_ssa_cli("jev", "classify", "--brief", str(brief), env=jev_env(te, fake))
        finally:
            fake.close()
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual((got["size"], got["difficulty"], got["kind"]), ("small", "hard", "debug"))
        self.assertEqual(got["flags"], "--size small --difficulty hard --kind debug")
        self.assertEqual(got["low_confidence"], [])
        sent = fake.requests[0]
        self.assertEqual(sent["auth"], "Bearer test-key")
        self.assertEqual(sent["body"]["state"], {"brief": GOOD_BRIEF})
        self.assertEqual(sent["body"]["questions"]["size"]["criteria"], list(jev.SIZE_LEVELS.values()))

    def test_low_confidence_is_named_and_unknown_kind_falls_back(self):
        fake = FakeJev(
            {
                "size": {"confidence": 0.3, "probabilities": {"2": 0.4, "3": 0.35}},
                "difficulty": {"confidence": 0.9, "probabilities": {"1": 0.9}},
                "kind": {"choice": "not-a-kind", "confidence": 0.2},
            }
        )
        try:
            with temp_env() as te:
                rc, out, _ = run_ssa(
                    "jev", "classify", "--brief", "-", env=jev_env(te, fake), input_text=GOOD_BRIEF
                )
        finally:
            fake.close()
        self.assertEqual(rc, 0)
        got = json.loads(out)
        self.assertEqual((got["size"], got["kind"]), ("medium", "default"))
        self.assertEqual(got["low_confidence"], ["kind", "size"])
        self.assertEqual(got["runner_up"], {"size": "large"})

    def test_difficulty_has_a_stricter_bar_and_names_its_rival(self):
        # Measured: hard at 0.72 raised the quota floor and left a task with no
        # eligible worker, while routine dispatched. 0.72 clears the general
        # 0.5 bar, so difficulty carries its own.
        fake = FakeJev(
            {
                "size": {"confidence": 0.72, "probabilities": {"2": 0.72, "1": 0.28}},
                "difficulty": {"confidence": 0.72, "probabilities": {"2": 0.72, "1": 0.28}},
                "kind": {"choice": "debug", "confidence": 1.0},
            }
        )
        try:
            with temp_env() as te:
                rc, out, _ = run_ssa(
                    "jev", "classify", "--brief", "-", env=jev_env(te, fake), input_text=GOOD_BRIEF
                )
        finally:
            fake.close()
        self.assertEqual(rc, 0)
        got = json.loads(out)
        self.assertEqual(got["difficulty"], "hard")
        self.assertEqual(got["effective"]["difficulty"], "routine")
        self.assertEqual(got["flags"], "--size medium --difficulty routine --kind debug")
        self.assertEqual(got["low_confidence"], ["difficulty"])
        self.assertEqual(got["runner_up"], {"difficulty": "routine"})
        self.assertGreater(jev.LOW_CONFIDENCE_BY["difficulty"], jev.LOW_CONFIDENCE)

    def test_effective_never_upshifts(self):
        got = jev.effective_class(
            "small", "routine", "impl", ["size"], {"size": "large"}
        )
        self.assertEqual(got["size"], "small")
        got = jev.effective_class(
            "medium", "hard", "impl", ["difficulty"], {"difficulty": "frontier"}
        )
        self.assertEqual(got["difficulty"], "hard")


class Lint(unittest.TestCase):
    def test_complete_brief_passes(self):
        fake = FakeJev(all_yes())
        try:
            with temp_env() as te:
                rc, out, _ = run_ssa("jev", "lint", env=jev_env(te, fake), input_text=GOOD_BRIEF)
        finally:
            fake.close()
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["missing"], [])

    def test_missing_elements_exit_1_and_workdir_is_checked_in_code(self):
        answers = all_yes()
        answers["acceptance"] = {"noul": 0.1}
        answers["needs_answers"] = {"noul": 0.8}
        fake = FakeJev(answers)
        try:
            with temp_env() as te:
                rc, out, _ = run_ssa(
                    "jev", "lint", env=jev_env(te, fake), input_text="Make the thing. Test it.\n"
                )
        finally:
            fake.close()
        self.assertEqual(rc, 1)
        got = json.loads(out)
        self.assertEqual(got["missing"], ["acceptance", "workdir", "structural"])
        self.assertTrue(any(w.startswith("needs_answers") for w in got["warnings"]))

    def test_lint_names_the_section_dispatch_would_refuse_over(self):
        # A brief that lints clean must be a brief that dispatches: the
        # structural gate is code in dispatch, so lint checks it in code too.
        bare = GOOD_BRIEF.split("\n## Structural")[0]
        for text, legacy, want in (
            (bare, None, ["structural"]),
            (bare + "\n## Structural discovery\n", None, ["structural"]),
            (bare, "1", []),
            (GOOD_BRIEF, None, []),
        ):
            fake = FakeJev(all_yes())
            try:
                with temp_env() as te:
                    extra = {"SSA_STRUCTURAL_LEGACY": legacy} if legacy else {}
                    _rc, out, _ = run_ssa("jev", "lint", env=jev_env(te, fake, **extra), input_text=text)
            finally:
                fake.close()
            self.assertEqual(json.loads(out)["missing"], want, text[-40:])


class Review(unittest.TestCase):
    """Post-run diff review: only existing test files, advisory, fails open."""

    def test_flags_a_flipped_assertion_and_sends_only_test_hunks(self):
        fake = FakeJev({"inverts_assertion": {"noul": 0.94}, "loosens_threshold": {"noul": 0.1}})
        try:
            with temp_env() as te:
                rc, out, _ = run_ssa("jev", "review", env=jev_env(te, fake), input_text=FLIPPED)
        finally:
            fake.close()
        self.assertEqual(rc, 1, out)
        got = json.loads(out)
        self.assertEqual(got["flags"], ["inverts_assertion"])
        self.assertEqual(got["files"], ["tests/test_scene.py"])
        sent = fake.requests[0]["body"]
        self.assertIn("assertNotIn", sent["state"]["diff"])
        self.assertNotIn("src/scene.py", sent["state"]["diff"])
        self.assertNotIn("test_new.py", sent["state"]["diff"])
        self.assertEqual(set(sent["questions"]), set(jev.REVIEW_QUESTIONS))

    def test_report_flags_a_dismissed_encoded_decision(self):
        fake = FakeJev(
            {
                "inverts_assertion": {"noul": 0.94},
                "dismisses_encoded_decision": {"noul": 0.91},
                "claim_not_in_diff": {"noul": 0.04},
            }
        )
        try:
            with temp_env() as te:
                p = te.root / "last-msg.txt"
                p.write_text("test_spare_is_not_a_body is stale; spare scoop is a body now.\n")
                rc, out, _ = run_ssa(
                    "jev", "review", "--brief", "-", "--report", str(p),
                    env=jev_env(te, fake), input_text=FLIPPED,
                )
        finally:
            fake.close()
        self.assertEqual(rc, 1, out)
        got = json.loads(out)
        self.assertIn("dismisses_encoded_decision", got["flags"])
        sent = fake.requests[0]["body"]
        self.assertIn("stale", sent["state"]["report"])
        self.assertIn("dismisses_encoded_decision", sent["questions"])

    def test_claim_is_judged_on_the_full_file_list_not_the_test_hunks(self):
        # 1789805781-99629, 1789817331-54852: the claim question saw only the
        # test hunks, so every source change the report named was "not in diff".
        src = (
            "diff --git a/src/scene.py b/src/scene.py\n--- a/src/scene.py\n+++ b/src/scene.py\n"
            "@@ -1 +1 @@\n-NODES = []\n+NODES = ['spare']\n"
        )
        fake = FakeJev({"claim_not_in_diff": {"noul": 0.05}})
        try:
            with temp_env() as te:
                p = te.root / "last-msg.txt"
                p.write_text("changed src/scene.py, tests/test_scene.py and added docs/new.md\n")
                extra = te.root / "untracked.txt"
                extra.write_text("docs/new.md\n")
                rc, out, _ = run_ssa(
                    "jev", "review", "--brief", "-", "--report", str(p), "--files", str(extra),
                    env=jev_env(te, fake), input_text=src + FLIPPED,
                )
        finally:
            fake.close()
        self.assertEqual(rc, 0, out)
        sent = fake.requests[0]["body"]
        self.assertNotIn("src/scene.py", sent["state"]["diff"])
        files = sent["state"]["changed_files"]
        for name in ("src/scene.py", "tests/test_scene.py", "docs/new.md"):
            self.assertIn(name, files)
        self.assertIn("changed_files", sent["questions"]["claim_not_in_diff"]["instructions"])

    def test_huge_source_diff_is_never_sent_for_the_claim_question(self):
        # 1789681429-79029: a 157K diff clipped to 24K hid most claimed changes.
        big = "".join(
            "diff --git a/src/m%d.py b/src/m%d.py\n--- a/src/m%d.py\n+++ b/src/m%d.py\n@@ -1 +1 @@\n-x\n+%s\n"
            % (i, i, i, i, "y" * 2000) for i in range(40)
        )
        fake = FakeJev({"claim_not_in_diff": {"noul": 0.05}})
        try:
            with temp_env() as te:
                p = te.root / "last-msg.txt"
                p.write_text("changed src/m39.py\n")
                rc, out, _ = run_ssa(
                    "jev", "review", "--brief", "-", "--report", str(p),
                    env=jev_env(te, fake), input_text=big,
                )
        finally:
            fake.close()
        self.assertEqual(rc, 0, out)
        sent = fake.requests[0]["body"]
        self.assertNotIn("diff", sent["state"])
        self.assertIn("src/m39.py", sent["state"]["changed_files"])
        self.assertEqual(set(sent["questions"]), {"claim_not_in_diff"})

    def test_clean_test_diff_exits_0(self):
        fake = FakeJev({qid: {"noul": 0.03} for qid in jev.REVIEW_QUESTIONS})
        try:
            with temp_env() as te:
                rc, out, _ = run_ssa("jev", "review", env=jev_env(te, fake), input_text=FLIPPED)
        finally:
            fake.close()
        self.assertEqual(rc, 0, out)
        self.assertTrue(json.loads(out)["reviewed"])

    def test_no_existing_test_touched_means_no_call(self):
        src_only = FLIPPED.split("diff --git a/tests/test_scene.py")[0]
        fake = FakeJev()
        try:
            with temp_env() as te:
                rc, out, _ = run_ssa("jev", "review", env=jev_env(te, fake), input_text=src_only)
        finally:
            fake.close()
        self.assertEqual(rc, 0, out)
        self.assertFalse(json.loads(out)["reviewed"])
        self.assertEqual(fake.requests, [])

    def test_test_paths(self):
        for path, want in (
            ("tests/test_cable.py", True), ("pkg/ring_test.go", True), ("web/a.spec.ts", True),
            ("src/__tests__/x.js", True), ("src/contest/x.py", False), ("latest/run.py", False),
        ):
            self.assertEqual(bool(jev.TEST_PATH_RE.search(path)), want, path)


class VerifyReview(unittest.TestCase):
    """_ssa_jev_review: records, folds into outcome.json, warns, never fails."""

    def make_task(self, te):
        from helpers import make_git_repo

        repo = make_git_repo(
            te.root / "repo", {"tests/test_scene.py": "def test_x():\n    assert 'spare' not in NODES\n"}
        )
        (repo / "tests" / "test_scene.py").write_text("def test_x():\n    assert 'spare' in NODES\n")
        task = te.root / "task"
        task.mkdir()
        (task / "wt.txt").write_text(str(repo) + "\n")
        import subprocess

        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        (task / "base-sha.txt").write_text(sha + "\n")
        (task / "outcome.json").write_text(json.dumps({"verify": {"verdict": "pass"}}))
        return task

    def test_untracked_files_reach_the_claim_question_and_the_review_reaches_the_ledger(self):
        fake = FakeJev({"inverts_assertion": {"noul": 0.91}, "claim_not_in_diff": {"noul": 0.04}})
        try:
            with temp_env() as te:
                task = self.make_task(te)
                repo = Path((task / "wt.txt").read_text().strip())
                (repo / "brand_new.py").write_text("x = 1\n")
                (task / "last-msg.txt").write_text("added brand_new.py\n")
                rc, _out, err = run_ssa("jev", "review", "--dir", str(task), env=jev_env(te, fake))
                self.assertEqual(rc, 0, err)
                self.assertIn("brand_new.py", fake.requests[0]["body"]["state"]["changed_files"])
                rc, _out, err = run_ssa("record", "--dir", str(task), "--outcome", "verified-pass", env=te.env)
                self.assertEqual(rc, 0, err)
                ledger = Path(te.env["XDG_STATE_HOME"]) / "smart-subagents" / "outcomes.jsonl"
                row = json.loads(ledger.read_text().splitlines()[-1])
                self.assertEqual(row["jev"]["review_flags"], ["inverts_assertion"])
                self.assertEqual(row["jev"]["review_scores"]["claim_not_in_diff"], 0.04)
        finally:
            fake.close()

    def test_warns_and_folds_into_the_outcome_without_touching_the_verdict(self):
        fake = FakeJev({"inverts_assertion": {"noul": 0.91}})
        try:
            with temp_env() as te:
                task = self.make_task(te)
                rc, _out, err = run_ssa("jev", "review", "--dir", str(task), env=jev_env(te, fake))
                self.assertEqual(rc, 0, err)
                self.assertIn("inverts_assertion=0.91 in tests/test_scene.py", err)
                doc = json.loads((task / "outcome.json").read_text())
                self.assertEqual(doc["verify"]["verdict"], "pass")
                self.assertEqual(doc["jev_review"]["flags"], ["inverts_assertion"])
        finally:
            fake.close()

    def test_silent_when_jev_is_off_or_down(self):
        down = FakeJev(status=503)
        try:
            with temp_env() as te:
                task = self.make_task(te)
                for env in (te.env, jev_env(te, down)):
                    rc, _out, err = run_ssa("jev", "review", "--dir", str(task), env=env)
                    self.assertEqual(rc, 0, err)
                    self.assertEqual(err.strip(), "")
                    self.assertFalse((task / "diff-review.json").exists())
                    self.assertNotIn("jev_review", json.loads((task / "outcome.json").read_text()))
        finally:
            down.close()


class FailOpen(unittest.TestCase):
    def assert_unavailable(self, rc, out):
        self.assertEqual(rc, 2, out)
        self.assertFalse(json.loads(out)["available"])

    def test_disabled(self):
        with temp_env() as te:
            rc, out, _ = run_ssa("jev", "lint", env=te.env, input_text=GOOD_BRIEF)
        self.assert_unavailable(rc, out)

    def test_no_key(self):
        with temp_env() as te:
            env = dict(te.env, SSA_JEV="1")
            env.pop("TYPESAFE_API_KEY", None)
            env.pop("XDG_CONFIG_HOME", None)  # HOME is the temp home: no key file
            rc, out, _ = run_ssa("jev", "classify", env=env, input_text=GOOD_BRIEF)
        self.assert_unavailable(rc, out)

    def test_key_file_is_read(self):
        fake = FakeJev(all_yes())
        try:
            with temp_env() as te:
                cfg = te.root / "cfg" / "typesafe"
                cfg.mkdir(parents=True)
                (cfg / "env").write_text("export TYPESAFE_API_KEY='from-file'\n")
                env = jev_env(te, fake, XDG_CONFIG_HOME=str(te.root / "cfg"))
                env.pop("TYPESAFE_API_KEY")
                rc, _, _ = run_ssa("jev", "lint", env=env, input_text=GOOD_BRIEF)
        finally:
            fake.close()
        self.assertEqual(rc, 0)
        self.assertEqual(fake.requests[0]["auth"], "Bearer from-file")

    def test_server_error_and_unreachable(self):
        fake = FakeJev(status=500)
        try:
            with temp_env() as te:
                rc, out, _ = run_ssa("jev", "lint", env=jev_env(te, fake), input_text=GOOD_BRIEF)
                self.assert_unavailable(rc, out)
                env = jev_env(te, fake, SSA_JEV_URL="http://127.0.0.1:9/v1/systemone")
                rc, out, _ = run_ssa("jev", "lint", env=env, input_text=GOOD_BRIEF)
                self.assert_unavailable(rc, out)
        finally:
            fake.close()

    def test_one_retry_on_rate_limit(self):
        fake = FakeJev(all_yes(), fail_first=1)
        try:
            with temp_env() as te:
                rc, _, _ = run_ssa("jev", "lint", env=jev_env(te, fake), input_text=GOOD_BRIEF)
        finally:
            fake.close()
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.requests), 2)

    def test_empty_brief_is_not_judged(self):
        fake = FakeJev(all_yes())
        try:
            with temp_env() as te:
                rc, out, _ = run_ssa("jev", "lint", env=jev_env(te, fake), input_text="  \n")
        finally:
            fake.close()
        self.assert_unavailable(rc, out)
        self.assertEqual(fake.requests, [])


class DispatchPreflight(unittest.TestCase):
    """_ssa_jev_lint: writes brief-lint.json, warns, and never changes the exit."""

    def run_helper(self, te, env, brief_text):
        task = te.root / "task"
        task.mkdir()
        (task / "brief.md").write_text(brief_text)
        rc, out, err = run_ssa("jev", "preflight", "--dir", str(task), env=env)
        return task, rc, err

    def test_warns_and_records_without_failing(self):
        answers = all_yes()
        answers["verify"] = {"noul": 0.05}
        fake = FakeJev(answers)
        try:
            with temp_env() as te:
                task, rc, err = self.run_helper(te, jev_env(te, fake), GOOD_BRIEF)
                self.assertEqual(rc, 0, err)
                self.assertIn("missing verify", err)
                self.assertEqual(json.loads((task / "brief-lint.json").read_text())["missing"], ["verify"])
        finally:
            fake.close()

    def test_silent_when_jev_is_off(self):
        with temp_env() as te:
            task, rc, err = self.run_helper(te, te.env, GOOD_BRIEF)
            self.assertEqual(rc, 0, err)
            self.assertEqual(err.strip(), "")
            self.assertFalse((task / "brief-lint.json").exists())


if __name__ == "__main__":
    unittest.main()
