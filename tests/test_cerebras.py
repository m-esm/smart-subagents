"""The cerebras worker: parser, registry default, locators and the proxy.

Cerebras is opencode driven through scripts/opencode-cerebras. Its quota is
read from rate-limit response headers (fixtures under
tests/fixtures/providers/cerebras/), its registry entry is the first one
with `default_for`, and its log locators are pinned against a trimmed real
`opencode run --format json` log. The proxy test starts a fake upstream on
127.0.0.1 and checks the strip, the key injection and the streamed body.
No real network, no real key.
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (  # noqa: E402
    PROVIDERS_DIR,
    ROOT,
    load_fixture_json,
    load_ssa,
    load_usage_module,
    make_status,
    run_ssa_cli,
)
from test_recommend import IsolatedStateTestCase  # noqa: E402

CEREBRAS_FIXTURES = PROVIDERS_DIR / "cerebras"
PROXY = ROOT / "scripts" / "ssa" / "cerebras_proxy.py"


def by_name(windows, name):
    for w in windows:
        if w.name == name:
            return w
    raise AssertionError(f"no window named {name!r} among {[w.name for w in windows]}")


class CerebrasParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_healthy_builds_six_windows_and_scores_on_the_day_meter(self):
        fx = load_fixture_json("cerebras", "healthy.json")
        st = self.m.parse_cerebras_usage(fx["headers"])
        self.assertTrue(st.available)
        self.assertTrue(st.eligible)
        self.assertEqual(st.skip_reason, "")
        self.assertEqual(
            {w.name for w in st.windows},
            {
                "tokens_minute", "tokens_hour", "tokens_day",
                "requests_minute", "requests_hour", "requests_day",
            },
        )
        day = by_name(st.windows, "tokens_day")
        self.assertEqual(day.limit, 720_000_000)
        self.assertEqual(day.used, 6_000_000)
        self.assertEqual(day.period_seconds, 86400.0)
        self.assertEqual(day.unit, "tokens")
        self.assertAlmostEqual(st.score, 100.0 - 6_000_000 / 720_000_000 * 100.0)
        self.assertEqual(by_name(st.windows, "tokens_minute").period_seconds, 60.0)
        self.assertTrue(st.extras["sandbox"])

    def test_header_names_match_case_insensitively(self):
        fx = load_fixture_json("cerebras", "healthy.json")
        upper = {k.upper(): v for k, v in fx["headers"].items()}
        st = self.m.parse_cerebras_usage(upper)
        self.assertTrue(st.eligible)
        self.assertEqual(len(st.windows), 6)

    def test_exhausted_day_window_blocks_eligibility(self):
        fx = load_fixture_json("cerebras", "exhausted_day.json")
        st = self.m.parse_cerebras_usage(fx["headers"])
        self.assertFalse(st.eligible)
        self.assertIn("tokens_day", st.skip_reason)
        self.assertEqual(st.score, 0.0)
        self.assertEqual(by_name(st.windows, "tokens_day").severity, "exhausted")

    def test_exhausted_minute_window_blocks_eligibility_but_keeps_score(self):
        fx = load_fixture_json("cerebras", "exhausted_minute.json")
        st = self.m.parse_cerebras_usage(fx["headers"])
        self.assertFalse(st.eligible)
        self.assertIn("tokens_minute", st.skip_reason)
        self.assertGreater(st.score, 99.0)

    def test_missing_day_window_is_missing_usage_not_full_headroom(self):
        fx = load_fixture_json("cerebras", "missing_day_window.json")
        st = self.m.parse_cerebras_usage(fx["headers"])
        self.assertTrue(st.available)
        self.assertFalse(st.eligible)
        self.assertEqual(st.skip_reason, self.m.USAGE_MISSING)
        self.assertEqual(st.score, 0.0)

    def test_malformed_numeric_degrades_with_a_warning(self):
        fx = load_fixture_json("cerebras", "malformed_numeric.json")
        st = self.m.parse_cerebras_usage(fx["headers"])
        self.assertFalse(st.eligible)
        self.assertTrue(st.extras.get("warnings"))

    def test_probe_is_registered(self):
        self.assertIs(self.m.PROBES["check_cerebras"], self.m.check_cerebras)
        self.assertIn("cerebras", self.m.WORKER_CLIS)


class RegistryDefaultTests(IsolatedStateTestCase):
    """workers.json `default_for` makes cerebras primary for cheap work."""

    def test_cerebras_is_primary_for_routine_when_it_has_quota(self):
        fleet = self.fleet({"cerebras": 60.0, "codex": 90.0, "grok": 85.0, "kimi": 80.0})
        rec = self.m.recommend(fleet, task_size="medium", difficulty="routine")
        self.assertEqual(rec["primary_worker"], "cerebras")
        self.assertEqual(rec["registry_default"], "cerebras")
        self.assertIn("registry default", " ".join(rec["reasons"]))
        self.assertEqual(rec["fallback_workers"][0], "codex")

    def test_cerebras_is_primary_for_trivial_too(self):
        fleet = self.fleet({"cerebras": 30.0, "codex": 90.0})
        rec = self.m.recommend(fleet, task_size="small", difficulty="trivial")
        self.assertEqual(rec["primary_worker"], "cerebras")

    def test_hard_and_frontier_never_land_on_cerebras(self):
        # hard has its own registry default (deepseek, tests/test_deepseek.py);
        # frontier has none and ranks normally.
        fleet = self.fleet({"cerebras": 100.0, "codex": 90.0, "grok": 85.0, "kimi": 80.0})
        for difficulty in ("hard", "frontier"):
            with self.subTest(difficulty=difficulty):
                rec = self.m.recommend(
                    fleet, task_size="medium", task_kind="impl", difficulty=difficulty
                )
                self.assertNotEqual(rec["registry_default"], "cerebras")
                self.assertNotEqual(rec["primary_worker"], "cerebras")
        rec = self.m.recommend(fleet, task_size="medium", task_kind="impl", difficulty="frontier")
        self.assertIsNone(rec["registry_default"])

    def test_default_yields_when_below_the_floor(self):
        fleet = self.fleet({"cerebras": 5.0, "codex": 90.0, "grok": 85.0})
        rec = self.m.recommend(fleet, task_size="medium", difficulty="routine")
        self.assertEqual(rec["primary_worker"], "codex")
        self.assertNotIn("registry default", " ".join(rec["reasons"]))

    def test_default_yields_when_exhausted_or_ineligible(self):
        fleet = self.fleet({"cerebras": 100.0, "codex": 90.0})
        for st in fleet:
            if st.cli == "cerebras":
                st.eligible = False
                st.skip_reason = "Cerebras window exhausted: tokens_day"
        rec = self.m.recommend(fleet, task_size="medium", difficulty="routine")
        self.assertEqual(rec["primary_worker"], "codex")

    def test_default_never_wins_a_relaxed_floor(self):
        thin = self.fleet({"cerebras": 8.0, "codex": 10.0, "grok": 8.0, "kimi": 12.0})
        rec = self.m.recommend(thin, task_size="medium", difficulty="routine")
        self.assertTrue(rec["floor_relaxed"])
        self.assertEqual(rec["primary_worker"], "kimi")

    def test_explicit_prefer_beats_the_registry_default(self):
        fleet = self.fleet({"cerebras": 100.0, "codex": 90.0, "grok": 85.0})
        rec = self.m.recommend(fleet, task_size="medium", difficulty="routine", prefer="grok")
        self.assertEqual(rec["primary_worker"], "grok")


class RegistryEntryTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_ssa("registry")

    def test_relative_candidate_resolves_beside_workers_json(self):
        reg = self.registry.load(cache=False)
        spec = reg.get("cerebras")
        self.assertEqual(spec.binary_candidates, ["opencode-cerebras"])
        resolved = spec.resolve_binary(env={"HOME": "/nonexistent", "PATH": ""})
        self.assertEqual(Path(resolved), ROOT / "scripts" / "opencode-cerebras")
        self.assertTrue(os.access(resolved, os.X_OK))

    def test_default_for_is_parsed_and_matched(self):
        reg = self.registry.load(cache=False)
        spec = reg.get("cerebras")
        self.assertEqual(spec.default_for, {"difficulty": ["trivial", "routine"]})
        self.assertTrue(spec.is_default_for("routine", "large"))
        self.assertFalse(spec.is_default_for("hard", "tiny"))
        self.assertEqual(reg.default_worker("trivial", "small"), "cerebras")
        self.assertEqual(reg.default_worker("frontier", "small"), "")
        self.assertIsNone(reg.get("codex").default_for)
        self.assertEqual(spec.to_dict()["default_for"], {"difficulty": ["trivial", "routine"]})

    def test_default_for_rejects_unknown_axis_and_bad_lists(self):
        base = json.loads((ROOT / "scripts" / "workers.json").read_text())["workers"]["cerebras"]
        for bad in ({"kind": ["impl"]}, {"difficulty": []}, {"difficulty": "routine"}, {}):
            block = dict(base)
            block["default_for"] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(self.registry.RegistryError):
                    self.registry.WorkerSpec("x", block)

    def test_model_used_is_read_from_the_short_model_flag(self):
        adapters = load_ssa("adapters")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "worker.txt").write_text("cerebras\n")
            Path(d, "worker-args.txt").write_text("--variant\nmedium\n-m\ncerebras/gpt-oss-120b\n")
            self.assertEqual(adapters.launched_model_for_dir(d), "cerebras/gpt-oss-120b")
            Path(d, "worker.txt").write_text("kimi\n")
            Path(d, "worker-args.txt").write_text("-m\nkimi-for-coding-highspeed\n")
            self.assertEqual(adapters.launched_model_for_dir(d), "kimi-for-coding-highspeed")

    def test_worker_args_pin_variant_and_model(self):
        m = load_usage_module()
        self.assertEqual(
            m.worker_args("cerebras", "routine", "medium"),
            ["--variant", "medium", "-m", "cerebras/gpt-oss-120b"],
        )
        self.assertEqual(m.worker_args("cerebras", "frontier", "large")[:2], ["--variant", "high"])


class LocatorTests(unittest.TestCase):
    """The registry's session/final/error locators against a real log shape."""

    def test_session_id_and_final_text(self):
        log = str(CEREBRAS_FIXTURES / "opencode-run.jsonl")
        rc, out, err = run_ssa_cli("parse-session", "--worker", "cerebras", "--log", log)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "ses_fixture000000000000000001")
        rc, out, err = run_ssa_cli("final-message", "--worker", "cerebras", "--log", log)
        self.assertEqual(rc, 0, err)
        self.assertIn("2 passed", out)

    def test_error_event_is_classified_from_the_log(self):
        adapters = load_ssa("adapters")
        digest = load_ssa("digest")
        registry = load_ssa("registry")
        log = str(CEREBRAS_FIXTURES / "opencode-run-error.jsonl")
        text = Path(log).read_text()
        # Generic classifier (feeds cooldowns) sees the nested opencode message.
        self.assertTrue(any("reasoning_content" in s for s in adapters.error_strings(text)))
        # Registry locator (feeds the digest) reads error.data.message.
        spec = registry.load(cache=False).get("cerebras")
        found = digest.error_message_from_text(text, "jsonl", spec.error)
        msg = found[0] if isinstance(found, tuple) else found
        self.assertIn("reasoning_content", msg or "")
        # A 400 is not a quota or auth failure, so it must not bench the worker.
        self.assertNotIn(adapters.classify_log(1, log), ("rate-limit", "auth"))


class _FakeUpstream(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        type(self).seen.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-ratelimit-remaining-tokens-day", "719999000")
        self.end_headers()
        for i in range(3):
            self.wfile.write(("data: chunk%d\n\n" % i).encode())
            self.wfile.flush()
            time.sleep(0.02)
        self.wfile.write(b"data: [DONE]\n\n")


class ProxyTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from ssa import cerebras_proxy

        self.proxy_mod = cerebras_proxy
        _FakeUpstream.seen = []
        self.upstream = http.server.HTTPServer(("127.0.0.1", 0), _FakeUpstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory(prefix="ssa-proxy-")
        self.port_file = os.path.join(self.tmp.name, "port")
        env = dict(os.environ)
        env["CEREBRAS_API_KEY"] = "test-key-not-real"
        self.proc = subprocess.Popen(
            [
                sys.executable, str(PROXY), "--port-file", self.port_file,
                "--upstream", "http://127.0.0.1:%d" % self.upstream.server_address[1],
            ],
            env=env,
            stderr=subprocess.PIPE,
        )
        for _ in range(100):
            if os.path.exists(self.port_file):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(self.port_file), "proxy never reported a port")
        self.port = int(Path(self.port_file).read_text().strip())

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        if self.proc.stderr:
            self.proc.stderr.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def test_rewrite_body_strips_only_assistant_reasoning(self):
        raw = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "x", "reasoning_content": "keep"},
                    {"role": "assistant", "content": "y", "reasoning_content": "r", "reasoning": "r2"},
                ]
            }
        ).encode()
        out = json.loads(self.proxy_mod.rewrite_body(raw))
        self.assertIn("reasoning_content", out["messages"][0])
        self.assertNotIn("reasoning_content", out["messages"][1])
        self.assertNotIn("reasoning", out["messages"][1])
        self.assertEqual(self.proxy_mod.rewrite_body(b"not json"), b"not json")
        self.assertEqual(self.proxy_mod.rewrite_body(b"[1,2]"), b"[1,2]")
        untouched = b'{"messages":[{"role":"assistant","content":"y"}]}'
        self.assertEqual(self.proxy_mod.rewrite_body(untouched), untouched)

    def test_round_trip_strips_injects_key_and_streams(self):
        body = json.dumps(
            {
                "model": "gpt-oss-120b",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "ok", "reasoning_content": "thinking"},
                ],
                "stream": True,
            }
        ).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % self.port,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer placeholder"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("x-ratelimit-remaining-tokens-day"), "719999000")
            streamed = resp.read().decode()
        self.assertEqual(streamed, "data: chunk0\n\ndata: chunk1\n\ndata: chunk2\n\ndata: [DONE]\n\n")
        self.assertEqual(len(_FakeUpstream.seen), 1)
        seen = _FakeUpstream.seen[0]
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer test-key-not-real")
        forwarded = json.loads(seen["body"])
        self.assertNotIn("reasoning_content", forwarded["messages"][1])
        self.assertEqual(forwarded["messages"][0]["content"], "hi")

    def test_health_endpoint(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=5) as resp:
            self.assertEqual(json.loads(resp.read()), {"ok": True})

    def test_key_file_is_read_when_env_is_unset(self):
        key_file = os.path.join(self.tmp.name, "env")
        Path(key_file).write_text("OTHER=1\nCEREBRAS_API_KEY='from-file'\n")
        saved = os.environ.pop("CEREBRAS_API_KEY", None)
        try:
            self.assertEqual(self.proxy_mod.read_key(key_file), "from-file")
            self.assertEqual(self.proxy_mod.read_key(os.path.join(self.tmp.name, "absent")), "")
        finally:
            if saved is not None:
                os.environ["CEREBRAS_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
