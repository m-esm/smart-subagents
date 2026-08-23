"""Characterization tests for the pure parse_*_usage() helpers.

These pin CURRENT behavior of scripts/ai-cli-usage.py, including behavior
that looks like a bug. Anywhere that is true, the assertion carries a
"characterization: current behavior" comment so Phase 3b can change it
deliberately instead of by accident.

No network, no credentials: every fixture is a static JSON file under
tests/fixtures/providers/<cli>/.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_fixture_json, load_usage_module  # noqa: E402


def by_name(windows, name):
    for w in windows:
        if w.name == name:
            return w
    raise AssertionError(f"no window named {name!r} among {[w.name for w in windows]}")


class ClaudeParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_healthy_all_window_kinds(self):
        fx = load_fixture_json("claude", "healthy.json")
        st = self.m.parse_claude_usage(fx["data"], fx["profile"])
        self.assertTrue(st.available)
        self.assertTrue(st.eligible)
        self.assertEqual(st.skip_reason, "")
        names = {w.name for w in st.windows}
        # five_hour, seven_day, seven_day_opus, seven_day_sonnet, plus the
        # model-scoped "Fable" limit.
        self.assertEqual(
            names,
            {"5h_session", "weekly_all", "weekly_opus", "weekly_sonnet", "weekly_Fable"},
        )
        self.assertAlmostEqual(by_name(st.windows, "5h_session").used_pct, 20.0)
        self.assertAlmostEqual(by_name(st.windows, "weekly_all").used_pct, 30.0)
        self.assertEqual(by_name(st.windows, "5h_session").period_seconds, 5 * 3600)
        self.assertEqual(by_name(st.windows, "weekly_all").period_seconds, 7 * 86400)
        # worst binding window is weekly_Fable's 40%, all well under any gate
        self.assertAlmostEqual(st.score, 60.0)
        self.assertEqual(st.plan, "individual")
        self.assertEqual(st.account, "tester@example.invalid")
        self.assertTrue(st.extras["local_labor"])

    def test_premium_window_critical_gates_local_labor_not_eligibility(self):
        fx = load_fixture_json("claude", "premium_window_critical.json")
        st = self.m.parse_claude_usage(fx["data"], fx["profile"])
        # A premium (Opus) weekly window at 92% keeps the CLI eligible but
        # marks it supervise-only: characterization: current behavior.
        self.assertTrue(st.eligible)
        self.assertIn("premium weekly window critical", st.skip_reason)
        self.assertFalse(st.extras["local_labor"])
        self.assertAlmostEqual(by_name(st.windows, "weekly_Opus").used_pct, 92.0)

    def test_exhausted_binding_window_blocks_eligibility(self):
        fx = load_fixture_json("claude", "exhausted.json")
        st = self.m.parse_claude_usage(fx["data"], fx["profile"])
        self.assertFalse(st.eligible)
        self.assertEqual(st.skip_reason, "Claude quota exhausted on a binding window")
        self.assertAlmostEqual(st.score, 0.4, places=3)

    def test_missing_fields_only_builds_present_windows(self):
        fx = load_fixture_json("claude", "missing_fields.json")
        st = self.m.parse_claude_usage(fx["data"], fx["profile"])
        self.assertEqual([w.name for w in st.windows], ["5h_session"])
        self.assertIsNone(by_name(st.windows, "5h_session").resets_in_hours)
        # No profile: account/plan stay whatever the caller's st carried in
        # (nothing, here), never populated by the usage payload itself.
        self.assertEqual(st.account, "")
        self.assertEqual(st.plan, "")

    def test_malformed_five_hour_utilization_raises(self):
        # characterization: current behavior: a non-numeric utilization is
        # not guarded and crashes the parser with ValueError.
        fx = load_fixture_json("claude", "malformed_numeric.json")
        with self.assertRaises(ValueError):
            self.m.parse_claude_usage(fx["data"], fx["profile"])

    def test_malformed_limit_percent_raises(self):
        # characterization: current behavior: same lack of guarding in the
        # model-scoped limits[] loop.
        fx = load_fixture_json("claude", "malformed_limit_percent.json")
        with self.assertRaises(ValueError):
            self.m.parse_claude_usage(fx["data"], fx["profile"])

    def test_st_argument_is_mutated_and_returned(self):
        # The extraction threads an existing CliStatus through so check_claude
        # can pre-populate plan/extras from the oauth token. Confirm identity
        # is preserved (same object returned).
        fx = load_fixture_json("claude", "healthy.json")
        base = self.m.CliStatus(cli="claude", available=False, plan="preset")
        base.extras["rate_limit_tier"] = "preset_tier"
        st = self.m.parse_claude_usage(fx["data"], fx["profile"], base)
        self.assertIs(st, base)
        self.assertTrue(st.available)
        # profile's organization_type overrides the preset plan when present.
        self.assertEqual(st.plan, "individual")


class CodexParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_healthy_primary_and_secondary_window(self):
        fx = load_fixture_json("codex", "healthy.json")
        st = self.m.parse_codex_usage(fx["data"])
        self.assertTrue(st.eligible)
        self.assertEqual({w.name for w in st.windows}, {"primary_window", "secondary_window"})
        self.assertAlmostEqual(by_name(st.windows, "primary_window").used_pct, 20.0)
        self.assertAlmostEqual(st.score, 80.0)
        self.assertEqual(st.extras["banked_resets"], 0)

    def test_relative_reset_after_seconds(self):
        fx = load_fixture_json("codex", "healthy.json")
        st = self.m.parse_codex_usage(fx["data"])
        # reset_after_seconds=3600 -> resets_in_hours is exactly 1.0, no
        # wall-clock dependency.
        self.assertAlmostEqual(by_name(st.windows, "primary_window").resets_in_hours, 1.0)

    def test_epoch_reset_matches_hours_until_helper(self):
        fx = load_fixture_json("codex", "epoch_reset.json")
        reset_at = fx["data"]["rate_limit"]["primary_window"]["reset_at"]
        before = self.m._hours_until(reset_at)
        st = self.m.parse_codex_usage(fx["data"])
        after = self.m._hours_until(reset_at)
        got = by_name(st.windows, "primary_window").resets_in_hours
        # Only reset_at (epoch) is present, no reset_after_seconds, so the
        # parser falls back to _hours_until(reset_at) exactly as the caller
        # would compute it moments apart.
        self.assertGreaterEqual(got, min(before, after) - 1e-6)
        self.assertLessEqual(got, max(before, after) + 1e-6)

    def test_near_exhausted_stays_eligible(self):
        fx = load_fixture_json("codex", "near_exhausted.json")
        st = self.m.parse_codex_usage(fx["data"])
        self.assertTrue(st.eligible)
        self.assertEqual(st.skip_reason, "")

    def test_exhausted_without_banked_resets(self):
        fx = load_fixture_json("codex", "exhausted_no_banked.json")
        st = self.m.parse_codex_usage(fx["data"])
        self.assertFalse(st.eligible)
        self.assertEqual(st.skip_reason, "Codex rate limit exhausted")

    def test_banked_resets_never_restore_eligibility(self):
        # characterization: current behavior: banked resets bump the score
        # to 5.0 (a small "not truly zero" signal) but eligible stays False.
        # Nothing auto-redeems a banked reset.
        fx = load_fixture_json("codex", "exhausted_with_banked.json")
        st = self.m.parse_codex_usage(fx["data"])
        self.assertFalse(st.eligible)
        self.assertIn("banked reset(s) available but do not auto-redeem", st.skip_reason)
        self.assertAlmostEqual(st.score, 5.0)

    def test_missing_rate_limit_defaults_to_fully_exhausted(self):
        # characterization: current behavior: no rate_limit block at all
        # means used_pct stays None, but the scoring step treats a missing
        # used_pct as used=100.0, i.e. "assume the worst", not "assume ok".
        fx = load_fixture_json("codex", "missing_fields.json")
        st = self.m.parse_codex_usage(fx["data"])
        self.assertIsNone(by_name(st.windows, "primary_window").used_pct)
        self.assertFalse(st.eligible)
        self.assertAlmostEqual(st.score, 0.0)

    def test_malformed_used_percent_raises(self):
        # characterization: current behavior: unlike kimi's usage parsing,
        # codex's used_percent -> float() conversion is not guarded.
        fx = load_fixture_json("codex", "malformed_used_percent.json")
        with self.assertRaises(ValueError):
            self.m.parse_codex_usage(fx["data"])

    def test_malformed_window_seconds_is_swallowed(self):
        # characterization: current behavior: limit_window_seconds is
        # guarded (_codex_window_seconds), so a bad value degrades to
        # period_seconds=None instead of raising.
        fx = load_fixture_json("codex", "malformed_window_seconds.json")
        st = self.m.parse_codex_usage(fx["data"])
        self.assertIsNone(by_name(st.windows, "primary_window").period_seconds)
        self.assertAlmostEqual(by_name(st.windows, "primary_window").used_pct, 10.0)


class GrokParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_root_and_nested_billing_shapes_agree(self):
        root_fx = load_fixture_json("grok", "healthy_root.json")
        nested_fx = load_fixture_json("grok", "healthy_nested.json")
        st_root = self.m.parse_grok_usage(root_fx["billing"], root_fx["user"])
        st_nested = self.m.parse_grok_usage(nested_fx["billing"], nested_fx["user"])
        self.assertAlmostEqual(st_root.score, st_nested.score)
        self.assertAlmostEqual(
            by_name(st_root.windows, "monthly_cli_credits").used_pct,
            by_name(st_nested.windows, "monthly_cli_credits").used_pct,
        )
        self.assertTrue(st_root.eligible)
        self.assertTrue(st_nested.eligible)

    def test_near_exhausted(self):
        fx = load_fixture_json("grok", "near_exhausted.json")
        st = self.m.parse_grok_usage(fx["billing"], fx["user"])
        self.assertTrue(st.eligible)
        self.assertAlmostEqual(st.score, 5.0)

    def test_exhausted_blocks_eligibility(self):
        fx = load_fixture_json("grok", "exhausted.json")
        st = self.m.parse_grok_usage(fx["billing"], fx["user"])
        self.assertFalse(st.eligible)
        self.assertEqual(st.skip_reason, "Grok monthly CLI credits exhausted")

    def test_missing_usage_fields_assumes_healthy(self):
        # characterization: current behavior: when monthlyLimit/used are
        # both absent, no window is built at all and the CLI is scored a
        # flat 50 and marked eligible, i.e. "unknown" reads as "assume ok",
        # the opposite default from codex's missing-rate-limit case.
        fx = load_fixture_json("grok", "missing_fields.json")
        st = self.m.parse_grok_usage(fx["billing"], fx["user"])
        self.assertEqual(st.windows, [])
        self.assertTrue(st.eligible)
        self.assertAlmostEqual(st.score, 50.0)

    def test_malformed_numeric_raises(self):
        # characterization: current behavior: monthlyLimit.val -> float()
        # is not guarded.
        fx = load_fixture_json("grok", "malformed_numeric.json")
        with self.assertRaises(ValueError):
            self.m.parse_grok_usage(fx["billing"], fx["user"])

    def test_has_grok_code_access_false_overrides_healthy_quota(self):
        # characterization: current behavior: hasGrokCodeAccess=false wins
        # even over an otherwise-healthy billing window: eligible is forced
        # False and score forced to 0.0 after the window was already scored.
        fx = load_fixture_json("grok", "no_code_access.json")
        st = self.m.parse_grok_usage(fx["billing"], fx["user"])
        self.assertFalse(st.eligible)
        self.assertEqual(st.skip_reason, "hasGrokCodeAccess=false")
        self.assertAlmostEqual(st.score, 0.0)
        # The window itself is still recorded with its real (healthy) numbers.
        self.assertAlmostEqual(by_name(st.windows, "monthly_cli_credits").used_pct, 1.0)

    def test_no_user_payload_skips_access_check(self):
        fx = load_fixture_json("grok", "no_user.json")
        st = self.m.parse_grok_usage(fx["billing"], fx["user"])
        self.assertNotIn("has_grok_code_access", st.extras)
        self.assertTrue(st.eligible)


class KimiParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_healthy_weekly_plus_throughput_windows(self):
        fx = load_fixture_json("kimi", "healthy.json")
        st = self.m.parse_kimi_usage(fx["data"], fx["me"])
        names = {w.name for w in st.windows}
        self.assertEqual(names, {"weekly_quota", "throughput_300m", "window_7_day"})
        self.assertAlmostEqual(by_name(st.windows, "weekly_quota").used_pct, 10.0)
        self.assertEqual(by_name(st.windows, "weekly_quota").period_seconds, 7 * 86400)
        self.assertEqual(by_name(st.windows, "throughput_300m").period_seconds, 300 * 60)
        self.assertEqual(by_name(st.windows, "window_7_day").period_seconds, 7 * 86400)
        self.assertEqual(st.account, "tester")
        self.assertEqual(st.plan, "Pro")
        self.assertEqual(st.extras["parallel_limit"], 3)
        self.assertFalse(st.extras["sandbox"])

    def test_near_exhausted(self):
        fx = load_fixture_json("kimi", "near_exhausted.json")
        st = self.m.parse_kimi_usage(fx["data"], fx["me"])
        self.assertTrue(st.eligible)
        self.assertAlmostEqual(st.score, 5.0)
        # No /me payload: falls back to membership.level for plan.
        self.assertEqual(st.plan, "pro")
        self.assertEqual(st.account, "")

    def test_exhausted_blocks_eligibility(self):
        fx = load_fixture_json("kimi", "exhausted.json")
        st = self.m.parse_kimi_usage(fx["data"], fx["me"])
        self.assertFalse(st.eligible)
        self.assertEqual(st.skip_reason, "Kimi weekly quota exhausted")

    def test_missing_limit_treated_as_fully_available(self):
        # characterization: current behavior: a missing usage.limit means
        # used_pct is never computed (stays None), and the scoring step
        # defaults the "used" value to 0.0, i.e. "assume fully available".
        # This is the opposite default from codex's missing-rate-limit case.
        fx = load_fixture_json("kimi", "missing_fields.json")
        st = self.m.parse_kimi_usage(fx["data"], fx["me"])
        self.assertIsNone(by_name(st.windows, "weekly_quota").used_pct)
        self.assertTrue(st.eligible)
        self.assertAlmostEqual(st.score, 100.0)

    def test_malformed_numeric_is_swallowed_not_raised(self):
        # characterization: current behavior: unlike claude/codex/grok,
        # kimi's usage try/except catches TypeError/ValueError and degrades
        # to used_pct=None instead of crashing, for both the top-level usage
        # block and each limits[] entry.
        fx = load_fixture_json("kimi", "malformed_numeric.json")
        st = self.m.parse_kimi_usage(fx["data"], fx["me"])
        self.assertIsNone(by_name(st.windows, "weekly_quota").used_pct)
        self.assertIsNone(by_name(st.windows, "throughput_300m").used_pct)
        self.assertTrue(st.eligible)


if __name__ == "__main__":
    unittest.main()
