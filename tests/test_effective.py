"""Characterization tests for effective_score / admission_score, apply_effective,
and the burn-rate forecast (forecast / annotate_forecasts).

Pure-function level: no shell, no cache, no state directory needed except
where forecast() reads the usage-history file, which is monkeypatched or
passed explicit `rows=` to stay hermetic.
"""

import os
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


class SharedReserveTests(unittest.TestCase):
    def setUp(self):
        self.m = load_usage_module()
        self._tmp = tempfile.TemporaryDirectory(prefix="ssa-reserve-")
        self.addCleanup(self._tmp.cleanup)
        orig_state_dir = self.m._state_dir
        self.m._state_dir = lambda: Path(self._tmp.name) / "smart-subagents"
        self.addCleanup(lambda: setattr(self.m, "_state_dir", orig_state_dir))
        self._flag_restore = []
        self.addCleanup(self._restore_flags)
        saved = {
            key: os.environ.get(key)
            for key in ("SSA_SHARED_RESERVE_PCT", "SSA_SHORT_HORIZON_HOURS")
        }
        self.addCleanup(lambda: self._restore_env(saved))

    def _restore_flags(self):
        for spec, previous in self._flag_restore:
            spec.shared_account = previous

    @staticmethod
    def _restore_env(saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _mark_shared(self, name):
        spec = self.m.REGISTRY.workers[name]
        self._flag_restore.append((spec, spec.shared_account))
        spec.shared_account = True

    def _short(self, used, remaining, resets, name="5h_session"):
        return make_window(
            self.m,
            name=name,
            used_pct=used,
            remaining_pct=remaining,
            resets_in_hours=resets,
            period_seconds=5 * 3600,
        )

    def _status(self, window, eligible=True, cli="grok"):
        return make_status(
            self.m, cli=cli, eligible=eligible, score=50.0, windows=[window]
        )

    def test_shipped_workers_default_the_flag_off(self):
        for name, spec in self.m.REGISTRY.workers.items():
            self.assertIs(spec.shared_account, False, name)

    def test_shared_window_at_39_is_ineligible_even_when_reset_is_minutes_away(self):
        window = self._short(61.0, 39.0, 0.05)
        st = self._status(window)
        self.m.apply_effective(st)
        before = st.effective_score
        self.assertAlmostEqual(self.m.effective_score(window), 99.2375, places=3)
        self.assertTrue(self.m.apply_shared_reserve(st, True))
        self.assertFalse(st.eligible)
        self.assertEqual(st.effective_score, before)
        self.assertAlmostEqual(st.effective_score, 99.2, places=1)
        self.assertIn("39", st.skip_reason)
        self.assertIn("5h_session", st.skip_reason)

    def test_shared_window_at_50_stays_eligible_with_the_short_window_discount(self):
        window = self._short(50.0, 50.0, 0.4)
        st = self._status(window)
        self.m.apply_effective(st)
        self.assertFalse(self.m.apply_shared_reserve(st, True))
        self.assertTrue(st.eligible)
        self.assertAlmostEqual(self.m.effective_score(window), 95.0, places=3)
        self.assertAlmostEqual(st.effective_score, 95.0, places=1)
        self.assertAlmostEqual(st.admission_score, 95.0, places=1)

    def test_shared_window_exactly_at_the_floor_stays_eligible(self):
        st = self._status(self._short(60.0, 40.0, 0.4))
        self.m.apply_effective(st)
        self.assertFalse(self.m.apply_shared_reserve(st, True))
        self.assertTrue(st.eligible)

    def test_personal_window_under_40_stays_eligible_when_the_flag_is_off(self):
        window = self._short(61.0, 39.0, 0.05)
        st = self._status(window)
        self.m.apply_effective(st)
        self.assertFalse(self.m.apply_shared_reserve(st, False))
        self.assertTrue(st.eligible)
        self.assertAlmostEqual(st.effective_score, 99.2, places=1)
        self.assertEqual(st.skip_reason, "")

    def test_personal_account_at_zero_stays_ineligible(self):
        window = self._short(100.0, 0.0, 0.4)
        st = self._status(window, eligible=False)
        self.m.apply_effective(st)
        self.assertAlmostEqual(self.m.effective_score(window), 90.0, places=3)
        self.assertAlmostEqual(st.effective_score, 90.0, places=1)
        self.assertFalse(self.m.apply_shared_reserve(st, False))
        self.assertFalse(st.eligible)
        self.assertEqual(st.skip_reason, "")

    def test_config_cannot_lower_the_reserve_floor(self):
        os.environ["SSA_SHARED_RESERVE_PCT"] = "10"
        self.assertEqual(self.m.shared_reserve_pct(), 40.0)
        low = self._status(self._short(61.0, 39.0, 0.05))
        self.m.apply_effective(low)
        self.assertTrue(self.m.apply_shared_reserve(low, True))
        self.assertFalse(low.eligible)
        mid = self._status(self._short(50.0, 50.0, 0.4))
        self.m.apply_effective(mid)
        self.assertFalse(self.m.apply_shared_reserve(mid, True))
        self.assertTrue(mid.eligible)
        self.assertAlmostEqual(mid.effective_score, 95.0, places=1)

    def test_config_can_raise_the_reserve_floor(self):
        os.environ["SSA_SHARED_RESERVE_PCT"] = "60"
        self.assertEqual(self.m.shared_reserve_pct(), 60.0)
        st = self._status(self._short(50.0, 50.0, 0.4))
        self.m.apply_effective(st)
        before = st.effective_score
        self.assertTrue(self.m.apply_shared_reserve(st, True))
        self.assertFalse(st.eligible)
        self.assertEqual(st.effective_score, before)
        self.assertAlmostEqual(st.effective_score, 95.0, places=1)

    def test_invalid_reserve_config_stays_at_the_floor(self):
        for raw in ("", "nope", "nan", "NaN", "inf", "-inf", "-5"):
            with self.subTest(raw=raw):
                os.environ["SSA_SHARED_RESERVE_PCT"] = raw
                self.assertEqual(self.m.shared_reserve_pct(), 40.0)

    def test_missing_reserve_config_stays_at_the_floor(self):
        os.environ.pop("SSA_SHARED_RESERVE_PCT", None)
        self.assertEqual(self.m.shared_reserve_pct(), 40.0)

    def test_shared_window_without_a_reading_is_not_blocked(self):
        window = make_window(
            self.m,
            name="5h_session",
            used_pct=None,
            remaining_pct=None,
            resets_in_hours=0.05,
            period_seconds=5 * 3600,
        )
        st = self._status(window)
        self.assertFalse(self.m.apply_shared_reserve(st, True))
        self.assertTrue(st.eligible)

    def test_reserve_derives_remaining_from_used_pct(self):
        window = make_window(
            self.m,
            name="5h_session",
            used_pct=61.0,
            remaining_pct=None,
            resets_in_hours=0.05,
            period_seconds=5 * 3600,
        )
        st = self._status(window)
        self.assertTrue(self.m.apply_shared_reserve(st, True))
        self.assertFalse(st.eligible)

    def test_non_binding_claude_window_does_not_trip_the_reserve(self):
        st = make_status(
            self.m,
            cli="claude",
            eligible=True,
            windows=[
                self._short(90.0, 10.0, 0.05, name="sonnet_session"),
                make_window(
                    self.m,
                    name="weekly_all",
                    used_pct=20.0,
                    remaining_pct=80.0,
                    resets_in_hours=100.0,
                    period_seconds=7 * 86400,
                ),
            ],
        )
        self.m.apply_effective(st)
        self.assertFalse(self.m.apply_shared_reserve(st, True))
        self.assertTrue(st.eligible)

    def test_any_binding_window_under_the_floor_blocks(self):
        st = make_status(
            self.m,
            cli="codex",
            eligible=True,
            windows=[
                self._short(50.0, 50.0, 0.4, name="primary_window"),
                make_window(
                    self.m,
                    name="secondary_window",
                    used_pct=70.0,
                    remaining_pct=30.0,
                    resets_in_hours=100.0,
                    period_seconds=7 * 86400,
                ),
            ],
        )
        self.assertTrue(self.m.apply_shared_reserve(st, True))
        self.assertFalse(st.eligible)
        self.assertIn("secondary_window", st.skip_reason)

    def test_reserve_does_not_replace_an_existing_skip_reason(self):
        st = self._status(self._short(61.0, 39.0, 0.05), eligible=False)
        st.skip_reason = "cooldown (rate-limit, 5 min left)"
        self.assertTrue(self.m.apply_shared_reserve(st, True))
        self.assertFalse(st.eligible)
        self.assertEqual(st.skip_reason, "cooldown (rate-limit, 5 min left)")

    def test_recommend_drops_a_shared_account_under_the_floor_at_every_size(self):
        self._mark_shared("grok")
        sizes = ("tiny", "small", "medium", "large")
        difficulties = ("trivial", "routine", "hard", "frontier")
        for size in sizes:
            for difficulty in difficulties:
                with self.subTest(size=size, difficulty=difficulty):
                    st = self._status(self._short(61.0, 39.0, 0.05))
                    rec = self.m.recommend(
                        [st], task_size=size, difficulty=difficulty
                    )
                    self.assertIsNone(rec["primary_worker"])
                    self.assertNotIn("grok", {row["cli"] for row in rec["ranked"]})
                    self.assertFalse(st.eligible)
                    self.assertTrue(
                        any("shared account reserve" in reason for reason in rec["reasons"])
                    )

    def test_recommend_keeps_the_discount_when_a_shared_account_stays_eligible(self):
        self._mark_shared("grok")
        for difficulty in ("trivial", "frontier"):
            with self.subTest(difficulty=difficulty):
                st = self._status(self._short(50.0, 50.0, 0.4))
                rec = self.m.recommend(
                    [st], task_size="small", difficulty=difficulty
                )
                self.assertEqual(rec["primary_worker"], "grok")
                self.assertTrue(st.eligible)
                self.assertAlmostEqual(rec["ranked"][0]["effective_score"], 95.0)
                self.assertAlmostEqual(st.effective_score, 95.0, places=1)


if __name__ == "__main__":
    unittest.main()
