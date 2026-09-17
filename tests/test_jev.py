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
)


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
        self.assertEqual(got["missing"], ["acceptance", "workdir"])
        self.assertTrue(any(w.startswith("needs_answers") for w in got["warnings"]))


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
