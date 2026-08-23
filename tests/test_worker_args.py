"""Table-driven characterization of worker_args(cli, difficulty, task_size).

Pins the full codex x grok x kimi x {trivial,routine,hard,frontier} x
{tiny,small,medium,large} matrix, including the codex frontier clamp to
"high" (codex's effort ladder has no "xhigh" rung), the grok "xhigh" rung,
and kimi's highspeed-alias rule (task_size only matters for kimi).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_usage_module  # noqa: E402


SIZES = ("tiny", "small", "medium", "large")
DIFFICULTIES = ("trivial", "routine", "hard", "frontier")

# cli -> difficulty -> expected worker_args, independent of task_size except
# for kimi (see KIMI_MATRIX below).
CODEX_MATRIX = {
    "trivial": ["-c", "model_reasoning_effort=low"],
    "routine": ["-c", "model_reasoning_effort=medium"],
    "hard": ["-c", "model_reasoning_effort=high"],
    # frontier wants "xhigh"; codex's ladder tops out at "high", so it clamps.
    "frontier": ["-c", "model_reasoning_effort=high"],
}

GROK_MATRIX = {
    "trivial": ["--reasoning-effort", "low"],
    "routine": ["--reasoning-effort", "medium"],
    "hard": ["--reasoning-effort", "high"],
    "frontier": ["--reasoning-effort", "xhigh"],
}

# kimi has no effort flag at all; the only lever is a "highspeed" model alias,
# and it only fires for trivial difficulty, or (tiny|small) size at
# trivial|routine difficulty.
KIMI_HIGHSPEED = ["-m", "kimi-for-coding-highspeed"]


def kimi_expected(difficulty, size):
    cheap = difficulty == "trivial" or (
        size in ("tiny", "small") and difficulty in ("trivial", "routine")
    )
    return KIMI_HIGHSPEED if cheap else []


class WorkerArgsMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_codex_matrix(self):
        for difficulty in DIFFICULTIES:
            for size in SIZES:
                with self.subTest(difficulty=difficulty, size=size):
                    got = self.m.worker_args("codex", difficulty, size)
                    self.assertEqual(got, CODEX_MATRIX[difficulty])

    def test_grok_matrix(self):
        for difficulty in DIFFICULTIES:
            for size in SIZES:
                with self.subTest(difficulty=difficulty, size=size):
                    got = self.m.worker_args("grok", difficulty, size)
                    self.assertEqual(got, GROK_MATRIX[difficulty])

    def test_kimi_matrix(self):
        for difficulty in DIFFICULTIES:
            for size in SIZES:
                with self.subTest(difficulty=difficulty, size=size):
                    got = self.m.worker_args("kimi", difficulty, size)
                    self.assertEqual(got, kimi_expected(difficulty, size))

    def test_kimi_highspeed_fires_for_tiny_and_small_at_trivial_and_routine(self):
        for size in ("tiny", "small"):
            for difficulty in ("trivial", "routine"):
                with self.subTest(size=size, difficulty=difficulty):
                    self.assertEqual(
                        self.m.worker_args("kimi", difficulty, size), KIMI_HIGHSPEED
                    )

    def test_kimi_highspeed_does_not_fire_for_medium_or_large_routine(self):
        for size in ("medium", "large"):
            with self.subTest(size=size):
                self.assertEqual(self.m.worker_args("kimi", "routine", size), [])

    def test_kimi_highspeed_fires_for_trivial_regardless_of_size(self):
        for size in SIZES:
            with self.subTest(size=size):
                self.assertEqual(self.m.worker_args("kimi", "trivial", size), KIMI_HIGHSPEED)

    def test_unknown_difficulty_falls_back_to_routine(self):
        # worker_args() itself does not validate difficulty; DIFFICULTY.get()
        # with a fallback to "routine" happens inside the function.
        self.assertEqual(
            self.m.worker_args("codex", "not-a-real-difficulty", "medium"),
            CODEX_MATRIX["routine"],
        )

    def test_unknown_cli_returns_empty(self):
        self.assertEqual(self.m.worker_args("not-a-real-cli", "hard", "medium"), [])

    def test_clamp_effort_helper_matches_worker_args_for_codex_and_grok(self):
        for difficulty, effort in (
            ("trivial", "low"),
            ("routine", "medium"),
            ("hard", "high"),
            ("frontier", "xhigh"),
        ):
            with self.subTest(difficulty=difficulty):
                self.assertEqual(
                    self.m._clamp_effort("codex", effort),
                    CODEX_MATRIX[difficulty][-1].split("=")[-1],
                )
                self.assertEqual(self.m._clamp_effort("grok", effort), GROK_MATRIX[difficulty][-1])

    def test_clamp_effort_kimi_has_no_ladder(self):
        self.assertEqual(self.m._clamp_effort("kimi", "high"), "")


if __name__ == "__main__":
    unittest.main()
