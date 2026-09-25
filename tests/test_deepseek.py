"""The deepseek worker: money meter, registry default, wrapper profile, proxy.

DeepSeek is opencode driven through scripts/opencode-deepseek, a symlink to
scripts/opencode-worker. Its meter is a prepaid USD balance plus a daily spend
cap computed from a per-day snapshot. The proxy runs with --no-strip because
DeepSeek wants reasoning_content echoed inside a tool-call turn. No real
network, no real key.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ROOT, load_ssa, load_usage_module  # noqa: E402
from test_recommend import IsolatedStateTestCase  # noqa: E402

TODAY = "2026-09-25"


def balance(usd, available=True):
    return {
        "is_available": available,
        "balance_infos": [
            {"currency": "USD", "total_balance": str(usd), "granted_balance": "0.00",
             "topped_up_balance": str(usd)}
        ],
    }


class DeepseekParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def parse(self, payload, snapshot, cap=5.0, floor=1.0):
        return self.m.parse_deepseek_usage(payload, snapshot, TODAY, cap, floor)

    def test_first_reading_of_the_day_starts_the_snapshot(self):
        st, snap = self.parse(balance(49.94), None)
        self.assertEqual(snap, {"day": TODAY, "start": 49.94})
        self.assertTrue(st.eligible)
        self.assertEqual(st.score, 100.0)
        (w,) = st.windows
        self.assertEqual((w.name, w.unit, w.used, w.limit), ("spend_day", "usd", 0.0, 5.0))
        self.assertEqual(st.extras["balance_usd"], 49.94)

    def test_spend_since_the_snapshot_fills_the_day_window(self):
        st, snap = self.parse(balance(47.94), {"day": TODAY, "start": 49.94})
        self.assertEqual(snap["start"], 49.94)
        self.assertAlmostEqual(st.windows[0].used_pct, 40.0)
        self.assertAlmostEqual(st.score, 60.0)
        self.assertTrue(st.eligible)

    def test_day_cap_reached_blocks_eligibility(self):
        st, _ = self.parse(balance(44.90), {"day": TODAY, "start": 49.94})
        self.assertFalse(st.eligible)
        self.assertIn("daily spend cap", st.skip_reason)

    def test_new_day_and_top_up_restart_the_snapshot(self):
        _, snap = self.parse(balance(40.0), {"day": "2026-09-24", "start": 49.94})
        self.assertEqual(snap, {"day": TODAY, "start": 40.0})
        st, snap = self.parse(balance(90.0), {"day": TODAY, "start": 40.0})
        self.assertEqual(snap["start"], 90.0)
        self.assertEqual(st.windows[0].used, 0.0)

    def test_low_balance_and_unavailable_account_block(self):
        st, _ = self.parse(balance(0.5), None)
        self.assertFalse(st.eligible)
        self.assertIn("under the $1.00 floor", st.skip_reason)
        st, _ = self.parse(balance(20.0, available=False), None)
        self.assertFalse(st.eligible)
        self.assertIn("is_available=false", st.skip_reason)

    def test_no_usd_balance_is_missing_usage(self):
        st, snap = self.parse({"balance_infos": [{"currency": "CNY", "total_balance": "9"}]}, None)
        self.assertFalse(st.eligible)
        self.assertEqual(st.windows, [])
        self.assertEqual(snap, {})

    def test_zero_cap_means_no_spend_allowed(self):
        st, _ = self.parse(balance(49.0), None, cap=0.0)
        self.assertFalse(st.eligible)

    def test_probe_is_registered(self):
        self.assertIs(self.m.PROBES["check_deepseek"], self.m.check_deepseek)
        self.assertIn("deepseek", self.m.WORKER_CLIS)


class DeepseekRoutingTests(IsolatedStateTestCase):
    def test_deepseek_is_primary_for_hard_when_it_has_quota(self):
        fleet = self.fleet({"deepseek": 70.0, "cerebras": 100.0, "codex": 90.0, "grok": 85.0})
        rec = self.m.recommend(fleet, task_size="medium", task_kind="impl", difficulty="hard")
        self.assertEqual(rec["primary_worker"], "deepseek")
        self.assertEqual(rec["registry_default"], "deepseek")

    def test_cheap_work_stays_on_cerebras(self):
        fleet = self.fleet({"deepseek": 100.0, "cerebras": 60.0, "codex": 90.0})
        rec = self.m.recommend(fleet, task_size="medium", difficulty="routine")
        self.assertEqual(rec["primary_worker"], "cerebras")

    def test_spent_cap_hands_hard_work_back_to_the_ranking(self):
        fleet = self.fleet({"deepseek": 100.0, "codex": 90.0, "grok": 85.0})
        for st in fleet:
            if st.cli == "deepseek":
                st.eligible = False
                st.skip_reason = "DeepSeek: daily spend cap $5.00 reached"
        rec = self.m.recommend(fleet, task_size="medium", task_kind="impl", difficulty="hard")
        self.assertNotEqual(rec["primary_worker"], "deepseek")

    def test_frontier_has_no_registry_default(self):
        fleet = self.fleet({"deepseek": 100.0, "codex": 90.0})
        rec = self.m.recommend(fleet, task_size="medium", task_kind="impl", difficulty="frontier")
        self.assertIsNone(rec["registry_default"])


class DeepseekRegistryTests(unittest.TestCase):
    def test_entry_resolves_the_symlinked_wrapper(self):
        reg = load_ssa("registry").load(cache=False)
        spec = reg.get("deepseek")
        resolved = Path(spec.resolve_binary(env={"HOME": "/nonexistent", "PATH": ""}))
        self.assertEqual(resolved, ROOT / "scripts" / "opencode-deepseek")
        self.assertEqual(resolved.resolve(), (ROOT / "scripts" / "opencode-worker").resolve())
        self.assertEqual(spec.default_for, {"difficulty": ["hard"]})
        self.assertEqual(reg.default_worker("hard", "large"), "deepseek")

    def test_flash_for_cheap_work_and_v4_pro_for_hard(self):
        m = load_usage_module()
        self.assertEqual(m.worker_args("deepseek", "routine", "medium")[-2:],
                         ["-m", "deepseek/deepseek-flash"])
        self.assertEqual(m.worker_args("deepseek", "hard", "medium")[-2:],
                         ["-m", "deepseek/deepseek-v4-pro"])
        self.assertEqual(m.worker_args("deepseek", "frontier", "large")[-2:],
                         ["-m", "deepseek/deepseek-v4-pro"])


class WrapperProfileTests(unittest.TestCase):
    def run_wrapper(self, name, *args, env_extra=None):
        with tempfile.TemporaryDirectory() as d:
            env = {"PATH": os.environ["PATH"], "HOME": d, "OPENCODE_BIN": "/bin/true"}
            env.update(env_extra or {})
            return subprocess.run(
                [str(ROOT / "scripts" / name), "--dir", d, *args],
                env=env, capture_output=True, text=True, timeout=30,
            )

    def test_unknown_profile_is_refused(self):
        r = self.run_wrapper("opencode-worker", env_extra={"OPENCODE_WORKER_PROFILE": "nope"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown profile", r.stderr)

    def test_a_model_name_with_shell_characters_is_refused(self):
        r = self.run_wrapper("opencode-deepseek", "-m", "deepseek/x;rm")
        self.assertEqual(r.returncode, 2)
        self.assertIn("refusing model name", r.stderr)

    def test_missing_key_stops_before_opencode_runs(self):
        r = self.run_wrapper("opencode-deepseek", "-m", "deepseek/deepseek-flash")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no DEEPSEEK_API_KEY", r.stderr)


class ProxyProfileTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from ssa import cerebras_proxy

        self.p = cerebras_proxy

    def test_no_strip_forwards_the_body_byte_for_byte(self):
        raw = json.dumps({"messages": [{"role": "assistant", "content": "y",
                                        "reasoning_content": "r"}]}).encode()
        self.assertEqual(self.p.rewrite_body(raw, ()), raw)
        self.assertNotIn(b"reasoning_content", self.p.rewrite_body(raw))

    def test_key_name_selects_the_line(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "env")
            Path(f).write_text("CEREBRAS_API_KEY=c\nDEEPSEEK_API_KEY=d\n")
            saved = {k: os.environ.pop(k, None) for k in ("CEREBRAS_API_KEY", "DEEPSEEK_API_KEY")}
            try:
                self.assertEqual(self.p.read_key(f, "DEEPSEEK_API_KEY"), "d")
                self.assertEqual(self.p.read_key(f), "c")
            finally:
                for k, v in saved.items():
                    if v is not None:
                        os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
