"""Characterization tests for effective_score / admission_score, apply_effective,
and the burn-rate forecast (forecast / annotate_forecasts).

Pure-function level: no shell, no cache, no state directory needed except
where forecast() reads the usage-history file, which is monkeypatched or
passed explicit `rows=` to stay hermetic.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_usage_module, make_status, make_window  # noqa: E402


class EffectiveScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_short_window_discounts_by_reset_horizon(self):
        # 90% used, resets in 0.4h out of a 4h horizon -> frac = 0.1, so the
        # penalty is 90 * 0.1 = 9, leaving effective = 91.
        w = make_window(
            self.m, name="5h_session", used_pct=90.0, remaining_pct=10.0,
            resets_in_hours=0.4, period_seconds=5 * 3600,
        )
        self.assertAlmostEqual(self.m.effective_score(w), 91.0, places=3)
        self.assertEqual(self.m._effective_basis(w), "short")

    def test_short_window_at_full_horizon_gets_no_discount(self):
        # resets_in_hours >= SSA_SHORT_HORIZON_HOURS (default 4): frac clamps
        # to 1.0, so effective == 100 - used exactly, same as the raw case.
        w = make_window(
            self.m, name="5h_session", used_pct=90.0, remaining_pct=10.0,
            resets_in_hours=4.0, period_seconds=5 * 3600,
        )
        self.assertAlmostEqual(self.m.effective_score(w), 10.0, places=3)

    def test_short_window_without_resets_in_hours_gets_no_discount(self):
        w = make_window(
            self.m, name="5h_session", used_pct=90.0, remaining_pct=10.0,
            resets_in_hours=None, period_seconds=5 * 3600,
        )
        self.assertAlmostEqual(self.m.effective_score(w), 10.0, places=3)
        self.assertEqual(self.m._effective_basis(w), "raw")

    def test_long_window_ahead_of_pace_scores_lower_than_behind_pace(self):
        week = 7 * 86400
        ahead = make_window(  # 30% used, 90% of week remaining (10% elapsed)
            self.m, name="weekly_all", used_pct=30.0,
            resets_in_hours=0.9 * week / 3600.0, period_seconds=week,
        )
        behind = make_window(  # 50% used, 20% of week remaining (80% elapsed)
            self.m, name="weekly_all", used_pct=50.0,
            resets_in_hours=0.2 * week / 3600.0, period_seconds=week,
        )
        self.assertGreater(self.m.effective_score(behind), self.m.effective_score(ahead))
        self.assertEqual(self.m._effective_basis(behind), "pace")

    def test_long_window_exactly_on_pace_gets_no_penalty(self):
        week = 7 * 86400
        on_pace = make_window(
            self.m, name="weekly_all", used_pct=50.0,
            resets_in_hours=0.5 * week / 3600.0, period_seconds=week,
        )
        self.assertAlmostEqual(self.m.effective_score(on_pace), 100.0, places=3)

    def test_unknown_window_shape_falls_back_to_remaining_pct(self):
        w = make_window(self.m, name="mystery", used_pct=40.0, remaining_pct=60.0)
        self.assertAlmostEqual(self.m.effective_score(w), 60.0)
        self.assertEqual(self.m._effective_basis(w), "raw")

    def test_used_pct_none_falls_back_to_remaining_pct_or_fifty(self):
        w = make_window(self.m, name="mystery", used_pct=None, remaining_pct=70.0)
        self.assertAlmostEqual(self.m.effective_score(w), 70.0)
        w2 = make_window(self.m, name="mystery", used_pct=None, remaining_pct=None)
        self.assertAlmostEqual(self.m.effective_score(w2), 50.0)


class AdmissionScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_short_window_admission_matches_effective(self):
        w = make_window(
            self.m, name="5h_session", used_pct=90.0, remaining_pct=10.0,
            resets_in_hours=0.4, period_seconds=5 * 3600,
        )
        self.assertAlmostEqual(self.m.admission_score(w), self.m.effective_score(w))

    def test_long_window_admission_ignores_pace(self):
        week = 7 * 86400
        ahead = make_window(
            self.m, name="weekly_all", used_pct=30.0,
            resets_in_hours=0.9 * week / 3600.0, period_seconds=week,
        )
        # admission stays on raw remaining regardless of how far ahead of
        # pace the window is.
        self.assertAlmostEqual(self.m.admission_score(ahead), 70.0)


class ApplyEffectiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_binding_windows_take_the_worst_on_each_axis(self):
        st = make_status(
            self.m,
            cli="claude",
            windows=[
                make_window(self.m, name="5h_session", used_pct=10.0, period_seconds=5 * 3600, resets_in_hours=4.5),
                make_window(self.m, name="weekly_all", used_pct=60.0, period_seconds=7 * 86400, resets_in_hours=100.0),
            ],
        )
        self.m.apply_effective(st)
        self.assertIsNotNone(st.effective_score)
        self.assertIsNotNone(st.admission_score)
        # weekly_all's 40% remaining (raw) is worse than 5h_session's 90%.
        self.assertAlmostEqual(st.admission_score, 40.0)

    def test_non_claude_cli_binds_on_every_window(self):
        st = make_status(
            self.m,
            cli="codex",
            windows=[
                make_window(self.m, name="primary_window", used_pct=20.0, period_seconds=5 * 3600, resets_in_hours=4.5),
                make_window(self.m, name="secondary_window", used_pct=80.0, period_seconds=7 * 86400, resets_in_hours=100.0),
            ],
        )
        self.m.apply_effective(st)
        self.assertAlmostEqual(st.admission_score, 20.0)

    def test_eff_of_and_adm_of_fall_back_to_raw_score_when_unset(self):
        st = make_status(self.m, cli="grok", score=42.0)
        self.assertEqual(self.m.eff_of(st), 42.0)
        self.assertEqual(self.m.adm_of(st), 42.0)


class ForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_fewer_than_min_snapshots_returns_none(self):
        now = 1_700_000_000.0
        rows = [
            {"ts": now - 3600, "cli": "codex", "window": "thin", "used_pct": 10.0},
            {"ts": now - 1800, "cli": "codex", "window": "thin", "used_pct": 20.0},
        ]
        self.assertIsNone(self.m.forecast("codex", "thin", now=now, rows=rows))

    def test_rising_usage_with_enough_snapshots_forecasts_positive_hours(self):
        now = 1_700_000_000.0
        rows = [
            {"ts": now - (4 - i) * 1800, "cli": "grok", "window": "rising", "used_pct": pct}
            for i, pct in enumerate([10.0, 20.0, 30.0, 40.0])
        ]
        hours = self.m.forecast("grok", "rising", now=now, rows=rows)
        self.assertIsNotNone(hours)
        self.assertGreater(hours, 0)
        # slope is 20%/hour (30-min steps of +10%); 60% remaining / 20%/h = 3h.
        self.assertAlmostEqual(hours, 3.0, places=2)

    def test_straddling_a_reset_pairs_are_excluded_from_the_slope(self):
        # A window that drops (a reset happened) between two points must not
        # contribute a negative "slope" that would understate the burn rate.
        now = 1_700_000_000.0
        rows = [
            {"ts": now - 5400, "cli": "grok", "window": "resetting", "used_pct": 80.0},
            {"ts": now - 3600, "cli": "grok", "window": "resetting", "used_pct": 5.0},  # reset here
            {"ts": now - 1800, "cli": "grok", "window": "resetting", "used_pct": 15.0},
            {"ts": now, "cli": "grok", "window": "resetting", "used_pct": 25.0},
        ]
        hours = self.m.forecast("grok", "resetting", now=now, rows=rows)
        # Only the rising pairs after the reset (5 -> 15 -> 25, and 5 -> 25)
        # contribute; the straddling pair (80 -> 5) is dropped entirely.
        self.assertIsNotNone(hours)
        self.assertGreater(hours, 0)

    def test_lookback_window_excludes_old_snapshots(self):
        now = 1_700_000_000.0
        old = now - self.m.FORECAST_LOOKBACK_HOURS * 3600 - 1
        rows = [
            {"ts": old - 2000, "cli": "codex", "window": "old", "used_pct": 5.0},
            {"ts": old - 1000, "cli": "codex", "window": "old", "used_pct": 10.0},
            {"ts": old, "cli": "codex", "window": "old", "used_pct": 15.0},
        ]
        # All three points are older than the lookback window relative to `now`.
        self.assertIsNone(self.m.forecast("codex", "old", now=now, rows=rows))

    def test_flat_or_falling_usage_returns_none(self):
        now = 1_700_000_000.0
        rows = [
            {"ts": now - 5400, "cli": "codex", "window": "flat", "used_pct": 30.0},
            {"ts": now - 3600, "cli": "codex", "window": "flat", "used_pct": 30.0},
            {"ts": now - 1800, "cli": "codex", "window": "flat", "used_pct": 30.0},
        ]
        self.assertIsNone(self.m.forecast("codex", "flat", now=now, rows=rows))

    def test_annotate_forecasts_writes_the_field_and_reads_from_disk(self):
        m = self.m
        tmp = tempfile.TemporaryDirectory(prefix="ssa-forecast-")
        self.addCleanup(tmp.cleanup)
        orig_history_path = m._history_path
        m._history_path = lambda: Path(tmp.name) / "usage-history.jsonl"
        now = 1_700_000_000.0
        rows = [
            {"ts": now - (4 - i) * 1800, "cli": "grok", "window": "rising", "used_pct": pct}
            for i, pct in enumerate([10.0, 20.0, 30.0, 40.0])
        ]
        m._history_path().write_text(
            "".join(__import__("json").dumps(r) + "\n" for r in rows)
        )
        try:
            statuses = [
                make_status(
                    m, cli="grok", score=60.0,
                    windows=[
                        make_window(
                            m, name="rising", used_pct=40.0, remaining_pct=60.0,
                            resets_in_hours=48.0, period_seconds=7 * 86400,
                        )
                    ],
                )
            ]
            m.annotate_forecasts(statuses, now=now)
            self.assertIsNotNone(statuses[0].windows[0].forecast_exhausts_in_hours)
        finally:
            m._history_path = orig_history_path


if __name__ == "__main__":
    unittest.main()
