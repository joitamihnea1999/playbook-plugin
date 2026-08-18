#!/usr/bin/env python3
"""mindmap-optimize carries the claim-consistency lens (batch-5 finding).

StrataDB batch 5: owning node [10] said the migration slice shipped while
overview node [1] still said "still ahead" — a live contradiction invisible
to the path-based staleness scan. The optimize command's instructions must
direct the analysis at cross-node claim contradictions and the report must
have a section for them, so the lens cannot silently regress out of the
surface.

Run: python3 -m unittest tests.test_mindmap_consistency_lens
"""
from __future__ import annotations

import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
DOC = _HERE.parent / "plugins/playbook/commands/mindmap-optimize.md"


class ConsistencyLensOnSurface(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")

    def test_scan_step_present(self):
        self.assertIn("Claim consistency scan", self.text)
        # the method: multi-node claims compared, owner identified
        self.assertIn("MORE THAN ONE node", self.text)
        self.assertIn("owning node", self.text)

    def test_report_section_present(self):
        self.assertIn("### Claim Contradictions", self.text)

    def test_fix_direction_taught(self):
        # single-home doctrine: the fact lives in ONE node
        self.assertIn("cannot fork again", self.text)


if __name__ == "__main__":
    unittest.main()
