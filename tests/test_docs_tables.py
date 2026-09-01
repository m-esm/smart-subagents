"""Drift gate: the routing tables in the docs must match the code.

docs/ROUTING.md, README.md and agents/smart-subagents.md each restate the
difficulty ladder and the per-size quota floors by hand. Nothing derives them,
so the only thing stopping the docs from lying is this test. Every documented
row is parsed out of the markdown and compared to `DIFFICULTY` / `BASE_FLOOR`
in scripts/ai-cli-usage.py, and a mismatch names the exact row.

Fix the doc, not the assertion: the code is the source of truth.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ROOT, load_usage_module  # noqa: E402

ROUTING_MD = ROOT / "docs" / "ROUTING.md"
README_MD = ROOT / "README.md"
AGENT_MD = ROOT / "agents" / "smart-subagents.md"

# `trivial` | ... | 0.6x | no
_MULT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*x")
_TICKED_RE = re.compile(r"`([a-z]+)`")


def table_rows(text: str):
    """Every markdown table row, as a list of trimmed cells."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or all(set(c) <= set("-: ") for c in cells):
            continue
        yield cells


def difficulty_rows(path: Path):
    """(difficulty, cells) for every row whose first cell names a difficulty."""
    text = path.read_text()
    out = []
    for cells in table_rows(text):
        name = _TICKED_RE.fullmatch(cells[0])
        if name and name.group(1) in ("trivial", "routine", "hard", "frontier"):
            out.append((name.group(1), cells))
    return out


class DocTableDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def _check_difficulty_table(self, path: Path, expect_effort: bool):
        rows = difficulty_rows(path)
        self.assertEqual(
            [name for name, _ in rows],
            list(self.m.DIFFICULTY),
            msg="%s lists difficulties %s, code has %s"
            % (path.name, [n for n, _ in rows], list(self.m.DIFFICULTY)),
        )
        for name, cells in rows:
            effort, mult, cross = self.m.DIFFICULTY[name]
            joined = " | ".join(cells)
            found = _MULT_RE.search(joined)
            self.assertIsNotNone(
                found, "%s row %r states no floor multiplier: %s" % (path.name, name, joined)
            )
            self.assertAlmostEqual(
                float(found.group(1)),
                mult,
                msg="%s row %r says floor %sx, code says %sx"
                % (path.name, name, found.group(1), mult),
            )
            required = "required" in joined.lower()
            self.assertEqual(
                required,
                cross,
                msg="%s row %r says cross-review required=%s, code says %s"
                % (path.name, name, required, cross),
            )
            if expect_effort:
                ticked = _TICKED_RE.findall(joined)
                self.assertIn(
                    effort,
                    ticked,
                    msg="%s row %r names efforts %s, code targets %r"
                    % (path.name, name, ticked, effort),
                )

    def test_routing_md_difficulty_table_matches_the_code(self):
        self._check_difficulty_table(ROUTING_MD, expect_effort=True)

    def test_readme_difficulty_table_matches_the_code(self):
        self._check_difficulty_table(README_MD, expect_effort=True)

    def test_agent_md_difficulty_table_matches_the_code(self):
        # The agent file describes effort in prose ("lowest", "max"), so only
        # the floor multiplier and the cross-review flag are checkable.
        self._check_difficulty_table(AGENT_MD, expect_effort=False)

    def test_routing_md_base_floor_table_matches_the_code(self):
        documented = {}
        for cells in table_rows(ROUTING_MD.read_text()):
            name = _TICKED_RE.fullmatch(cells[0])
            if not name or name.group(1) not in self.m.BASE_FLOOR:
                continue
            if len(cells) < 2 or not cells[1].isdigit():
                continue
            documented[name.group(1)] = int(cells[1])
        self.assertEqual(
            documented,
            dict(self.m.BASE_FLOOR),
            msg="docs/ROUTING.md base floors %s, code has %s"
            % (documented, dict(self.m.BASE_FLOOR)),
        )


if __name__ == "__main__":
    unittest.main()
