"""Characterization tests for recommend() and its supporting machinery:
eligibility x quota floor x difficulty, admission vs effective headroom,
rank_basis, prefer, cooldowns, and the fit posterior.

These pin CURRENT behavior. State (cooldowns, ledger) lives under a fresh
temp XDG_STATE_HOME per test so tests never touch the real user's state.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_usage_module, make_status  # noqa: E402


class IsolatedStateTestCase(unittest.TestCase):
    """Points XDG_STATE_HOME at a fresh temp dir for the module under test."""

    def setUp(self):
        self.m = load_usage_module()
        self._tmp = tempfile.TemporaryDirectory(prefix="ssa-recommend-")
        self._state_home = Path(self._tmp.name)
        self._orig_state_dir = self.m._state_dir
        self.m._state_dir = lambda: self._state_home / "smart-subagents"

    def tearDown(self):
        self.m._state_dir = self._orig_state_dir
        self._tmp.cleanup()

    def fleet(self, scores: dict, **common):
        out = []
        for cli in self.m.WORKER_CLIS:
            val = scores.get(cli, 0.0)
            out.append(
                make_status(
                    self.m,
                    cli=cli,
                    available=True,
                    eligible=True,
                    score=val,
                    effective_score=val,
                    admission_score=val,
                    **common,
                )
            )
        return out


class FloorMatrixTests(IsolatedStateTestCase):
    """min_score = min(90, base_floor(size) * floor_mult(difficulty))."""

    BASE_FLOOR = {"tiny": 5, "small": 15, "medium": 25, "large": 40}
    FLOOR_MULT = {"trivial": 0.6, "routine": 1.0, "hard": 1.4, "frontier": 1.8}

    def expected_floor(self, size, difficulty):
        return min(90.0, self.BASE_FLOOR[size] * self.FLOOR_MULT[difficulty])

    def test_floor_applies_at_every_size_and_difficulty(self):
        for size in self.BASE_FLOOR:
            for difficulty in self.FLOOR_MULT:
                with self.subTest(size=size, difficulty=difficulty):
                    rec = self.m.recommend(
                        self.fleet({}), task_size=size, difficulty=difficulty
                    )
                    self.assertAlmostEqual(rec["min_score"], self.expected_floor(size, difficulty))

    def test_hard_and_frontier_never_relax_the_floor(self):
        thin = self.fleet({"codex": 10.0, "grok": 10.0, "kimi": 10.0})
        for difficulty in ("hard", "frontier"):
            with self.subTest(difficulty=difficulty):
                rec = self.m.recommend(thin, task_size="small", difficulty=difficulty)
                self.assertIsNone(rec["primary_worker"])
                self.assertFalse(rec["floor_relaxed"])
                self.assertIn("not dispatching on fumes", " ".join(rec["reasons"]))

    def test_trivial_and_routine_relax_when_everyone_is_below_floor(self):
        thin = self.fleet({"codex": 10.0, "grok": 8.0, "kimi": 12.0})
        for difficulty in ("trivial", "routine"):
            with self.subTest(difficulty=difficulty):
                rec = self.m.recommend(thin, task_size="medium", difficulty=difficulty)
                self.assertIsNotNone(rec["primary_worker"])
                self.assertTrue(rec["floor_relaxed"])

    def test_relaxation_still_picks_the_best_available(self):
        thin = self.fleet({"codex": 10.0, "grok": 8.0, "kimi": 12.0})
        rec = self.m.recommend(thin, task_size="medium", difficulty="routine")
        self.assertEqual(rec["primary_worker"], "kimi")

    def test_no_relaxation_when_at_least_one_survivor_clears_the_floor(self):
        mixed = self.fleet({"codex": 60.0, "grok": 5.0, "kimi": 5.0})
        rec = self.m.recommend(mixed, task_size="medium", difficulty="hard")
        self.assertFalse(rec["floor_relaxed"])
        self.assertEqual(rec["primary_worker"], "codex")


class RankBasisTests(IsolatedStateTestCase):
    def test_hard_and_frontier_rank_by_fit_others_by_headroom(self):
        for difficulty, expected in (
            ("trivial", "headroom"),
            ("routine", "headroom"),
            ("hard", "fit"),
            ("frontier", "fit"),
        ):
            with self.subTest(difficulty=difficulty):
                rec = self.m.recommend(
                    self.fleet({"codex": 60.0, "grok": 60.0, "kimi": 60.0}),
                    task_size="medium",
                    difficulty=difficulty,
                )
                self.assertEqual(rec["rank_basis"], expected)

    def test_fit_ranking_prefers_the_best_capability_prior_when_headroom_ties(self):
        # impl priors: codex 1.12, grok 1.05, kimi 0.95 (FIT["impl"])
        tied = self.fleet({"codex": 80.0, "grok": 80.0, "kimi": 80.0})
        rec = self.m.recommend(tied, task_size="medium", task_kind="impl", difficulty="hard")
        self.assertEqual(rec["primary_worker"], "codex")

    def test_headroom_ranking_prefers_more_quota_over_fit(self):
        mixed = self.fleet({"codex": 60.0, "grok": 50.0, "kimi": 90.0})
        rec = self.m.recommend(
            mixed, task_size="medium", task_kind="impl", difficulty="routine"
        )
        self.assertEqual(rec["primary_worker"], "kimi")


class AdmissionVsEffectiveTests(IsolatedStateTestCase):
    def test_pace_ahead_ranks_well_but_admission_reads_raw_capacity(self):
        starved = make_status(
            self.m,
            cli="codex",
            score=7.0,
            windows=[
                self.m.Window(
                    name="primary_window",
                    used_pct=93.0,
                    remaining_pct=7.0,
                    resets_in_hours=3.9 * 24,
                    period_seconds=7 * 86400,
                    severity="critical",
                )
            ],
        )
        self.m.apply_effective(starved)
        # 93% used but 93.9% of the week elapsed: pace says "fine".
        self.assertGreater(starved.effective_score, 21.0)
        # Admission never reads pace on a long window: 7% left funds nothing.
        self.assertAlmostEqual(starved.admission_score, 7.0)

        rec = self.m.recommend([starved], task_size="small", difficulty="hard")
        self.assertIsNone(rec["primary_worker"])
        self.assertAlmostEqual(rec["min_score"], 21.0)

    def test_five_hour_window_resetting_soon_is_real_capacity(self):
        refilling = make_status(
            self.m,
            cli="grok",
            score=10.0,
            windows=[
                self.m.Window(
                    name="5h_session",
                    used_pct=90.0,
                    remaining_pct=10.0,
                    resets_in_hours=0.4,
                    period_seconds=5 * 3600,
                    severity="critical",
                )
            ],
        )
        self.m.apply_effective(refilling)
        self.assertGreater(refilling.admission_score, 85.0)
        rec = self.m.recommend([refilling], task_size="small", difficulty="hard")
        self.assertEqual(rec["primary_worker"], "grok")
        self.assertGreater(rec["ranked"][0]["admission_score"], 85.0)


class PreferTests(IsolatedStateTestCase):
    def test_prefer_wins_when_it_survives_the_filter(self):
        mixed = self.fleet({"codex": 60.0, "grok": 50.0, "kimi": 90.0})
        rec = self.m.recommend(
            mixed, task_size="medium", task_kind="impl", difficulty="hard", prefer="grok"
        )
        self.assertEqual(rec["primary_worker"], "grok")

    def test_prefer_is_ignored_when_filtered_out_by_the_floor(self):
        # grok is well below the hard-difficulty floor at small size; the
        # preferred worker never enters `ranked_statuses`, so it cannot win.
        mixed = self.fleet({"codex": 60.0, "grok": 3.0, "kimi": 60.0})
        rec = self.m.recommend(
            mixed, task_size="small", task_kind="impl", difficulty="hard", prefer="grok"
        )
        self.assertNotEqual(rec["primary_worker"], "grok")

    def test_prefer_unknown_cli_is_silently_ignored(self):
        mixed = self.fleet({"codex": 60.0, "grok": 60.0, "kimi": 60.0})
        rec = self.m.recommend(
            mixed, task_size="medium", difficulty="routine", prefer="not-a-real-cli"
        )
        self.assertIsNotNone(rec["primary_worker"])
        self.assertNotEqual(rec["primary_worker"], "not-a-real-cli")


class CooldownTests(IsolatedStateTestCase):
    def test_active_cooldown_removes_a_worker_from_ranking(self):
        self.m.set_cooldown("codex", "rate-limit")
        rec = self.m.recommend(
            self.fleet({"codex": 90.0, "grok": 90.0, "kimi": 90.0}),
            task_size="medium",
            difficulty="routine",
        )
        self.assertNotEqual(rec["primary_worker"], "codex")
        self.assertIn("codex", rec["cooldowns"])
        self.assertNotIn("codex", {r["cli"] for r in rec["ranked"]})
        self.assertTrue(any("cooldown (rate-limit" in r for r in rec["reasons"]))

    def test_expired_cooldown_stops_gating(self):
        self.m.set_cooldown("codex", "rate-limit")
        path = self.m._cooldown_path()
        data = json.loads(path.read_text())
        data["codex"]["until"] = time.time() - 60
        path.write_text(json.dumps(data))
        rec = self.m.recommend(
            self.fleet({"codex": 90.0, "grok": 90.0, "kimi": 90.0}),
            task_size="medium",
            difficulty="routine",
        )
        self.assertEqual(rec["cooldowns"], {})
        self.assertIn("codex", {r["cli"] for r in rec["ranked"]})

    def test_clear_cooldown_removes_it(self):
        self.m.set_cooldown("grok", "auth")
        self.assertGreater(self.m.load_cooldowns()["grok"]["minutes_left"], 60)
        self.assertTrue(self.m.clear_cooldown("grok"))
        self.assertEqual(self.m.load_cooldowns(), {})

    def test_clearing_a_cooldown_that_does_not_exist_returns_false(self):
        self.assertFalse(self.m.clear_cooldown("kimi"))


class FitPosteriorTests(IsolatedStateTestCase):
    def _write_ledger(self, n, worker="codex", kind="impl", outcome="verified-pass"):
        ledger = self.m._ledger_path()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        lines = []
        for i in range(n):
            lines.append(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - i * 600)),
                        "worker": worker,
                        "kind": kind,
                        "outcome": outcome,
                        "retries": 0,
                    }
                )
            )
        ledger.write_text("\n".join(lines) + "\n")

    def test_below_min_samples_stays_advisory(self):
        self._write_ledger(3)
        value, n_eff, used = self.m.fit_posterior("codex", "impl")
        self.assertFalse(used)
        self.assertGreaterEqual(value, 0.85)
        self.assertLessEqual(value, 1.15)

    def test_at_or_above_min_samples_is_used(self):
        self._write_ledger(12)
        value, n_eff, used = self.m.fit_posterior("codex", "impl")
        self.assertTrue(used)
        self.assertGreaterEqual(n_eff, 10.0)
        self.assertGreaterEqual(value, 0.85)
        self.assertLessEqual(value, 1.15)

    def test_corrupt_ledger_line_is_skipped_not_fatal(self):
        ledger = self.m._ledger_path()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"worker": "codex"}\n{ not json\n')
        rows, skipped = self.m.read_ledger()
        # The well-formed line has no "ts", so it is skipped too: both lines
        # fail to parse into a usable row.
        self.assertEqual(skipped, 2)
        self.assertEqual(rows, [])

    def test_recommend_reports_fit_for_every_worker_kind_used_or_not(self):
        self._write_ledger(3)
        rec = self.m.recommend(
            self.fleet({"codex": 90.0, "grok": 90.0, "kimi": 90.0}),
            task_size="medium",
            task_kind="impl",
            difficulty="hard",
        )
        self.assertEqual(set(rec["fit"]), set(self.m.WORKER_CLIS))
        for info in rec["fit"].values():
            self.assertEqual({"prior", "posterior", "n_eff", "used"}, set(info))
        self.assertFalse(rec["fit"]["codex"]["used"])


class LocalLaborTests(IsolatedStateTestCase):
    """local_labor_ok fails closed: silence is never a licence to burn Opus."""

    def _claude(self, **extras):
        return make_status(
            self.m,
            cli="claude",
            score=80.0,
            effective_score=80.0,
            admission_score=80.0,
            extras=dict(extras),
        )

    def test_readable_healthy_claude_allows_local_labor(self):
        rec = self.m.recommend([self._claude(local_labor=True)], difficulty="routine")
        self.assertTrue(rec["local_labor_ok"])

    def test_unreadable_claude_meter_withholds_local_labor(self):
        # The probe errored, so extras is empty. `is not False` used to read
        # that as permission.
        rec = self.m.recommend([self._claude()], difficulty="routine")
        self.assertFalse(rec["local_labor_ok"])
        self.assertIn("claude usage unreadable: local labor withheld", rec["reasons"])

    def test_unavailable_claude_withholds_local_labor(self):
        st = self._claude(local_labor=True)
        st.available = False
        rec = self.m.recommend([st], difficulty="routine")
        self.assertFalse(rec["local_labor_ok"])

    def test_claude_absent_entirely_withholds_local_labor(self):
        # `--cli codex` produces a status list with no claude in it at all.
        only_codex = [
            make_status(
                self.m,
                cli="codex",
                score=80.0,
                effective_score=80.0,
                admission_score=80.0,
            )
        ]
        rec = self.m.recommend(only_codex, difficulty="routine")
        self.assertEqual(rec["primary_worker"], "codex")
        self.assertFalse(rec["local_labor_ok"])
        self.assertIn(
            "claude was not probed: local labor withheld until its meter is read",
            rec["reasons"],
        )

    def test_premium_window_still_withholds_even_with_the_flag_set(self):
        st = self._claude(local_labor=True)
        st.windows = [
            self.m.Window(name="weekly_Opus", used_pct=92.0, remaining_pct=8.0)
        ]
        rec = self.m.recommend([st], difficulty="routine")
        self.assertFalse(rec["local_labor_ok"])


class ReasonsTextTests(IsolatedStateTestCase):
    def test_reasons_include_the_floor_sentence_when_no_worker_clears_it(self):
        thin = self.fleet({"codex": 5.0, "grok": 5.0, "kimi": 5.0})
        rec = self.m.recommend(thin, task_size="large", difficulty="frontier")
        self.assertIsNone(rec["primary_worker"])
        joined = " ".join(rec["reasons"])
        self.assertIn("quota floor", joined)
        self.assertIn("not dispatching on fumes", joined)

    def test_no_eligible_worker_at_all_gets_a_distinct_reason(self):
        none_eligible = [
            make_status(self.m, cli=cli, available=False, eligible=False, score=0.0)
            for cli in self.m.WORKER_CLIS
        ]
        rec = self.m.recommend(none_eligible, task_size="medium", difficulty="routine")
        self.assertIsNone(rec["primary_worker"])
        self.assertIn(
            "no eligible external worker, all exhausted or unavailable", rec["reasons"]
        )


if __name__ == "__main__":
    unittest.main()
