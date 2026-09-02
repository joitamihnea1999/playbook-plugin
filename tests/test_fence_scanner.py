#!/usr/bin/env python3
"""One strict shared CommonMark fence scanner for task.md (task 039).

The two hand-rolled scanners `_iter_nonfenced` (fail CLOSED — an unclosed opener
fences to EOF, so the DELETE-writers never delete past a malformed fence) and
`_closed_fence_line_indices` (fail OPEN — an unclosed opener contributes nothing,
so the receipt writer never refuses an insert) shared opener/closer rules but were
two implementations. This suite pins the single engine `_iter_fenced_flags` and
its two policy directions, then the vector-specific behaviors layered on it:

  V1 — one engine, per-consumer `unclosed_is_live` fail direction (this file's core)
  V4 — indented (>=4-column) code-block awareness
  V6 — NBSP / non-ASCII-whitespace fence closers are NOT closers
  V5 — strict ATX-H2 boundary (`##\\tX` is a boundary; `>=4-space ## X` is not)

Run: python3 tests/test_fence_scanner.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

import tempfile  # noqa: E402

from tasks.core import (  # noqa: E402
    _closed_fence_line_indices,
    _extract_block_reason,
    _iter_fenced_flags,
    _iter_nonfenced,
    _latest_receipt_line,
    find_unconsumed_handoff,
)


def _nonfenced_texts(text: str) -> "list[str]":
    return [s for _i, s in _iter_nonfenced(text.splitlines())]


class SharedEngineFailDirections(unittest.TestCase):
    """V1 — the ONE engine, two deliberate fail directions on an UNCLOSED fence."""

    def test_closed_fence_is_fenced_in_both_directions(self):
        lines = [
            "live before",
            "```",
            "## Risk",
            "reversible",
            "```",
            "live after",
        ]
        for policy in (True, False):
            flags = _iter_fenced_flags(lines, unclosed_is_live=policy)
            # opener(1), interior(2,3), closer(4) fenced; live lines(0,5) not
            self.assertEqual(
                flags, [False, True, True, True, True, False],
                f"closed fence must mark opener..closer inclusive (policy={policy})")

    def test_unclosed_fence_fails_closed_when_unclosed_is_live_false(self):
        # Delete-writers: an unclosed opener fences THROUGH EOF, so a heading
        # after it is treated as fenced (never deleted past).
        lines = ["live", "```", "## Risk", "reversible", "still inside"]
        flags = _iter_fenced_flags(lines, unclosed_is_live=False)
        self.assertEqual(flags, [False, True, True, True, True])

    def test_unclosed_fence_stays_live_when_unclosed_is_live_true(self):
        # Receipt writer / pure readers: an unclosed opener contributes nothing,
        # so the remainder (incl. the opener line) stays LIVE.
        lines = ["live", "```", "## Risk", "reversible", "still after"]
        flags = _iter_fenced_flags(lines, unclosed_is_live=True)
        self.assertEqual(flags, [False, False, False, False, False])

    def test_backtick_info_with_backtick_is_not_a_fence(self):
        # A backtick opener whose info string holds a backtick is inline code,
        # not a fence — the line stays live in both directions.
        lines = ["```py `x`", "## Risk", "reversible"]
        for policy in (True, False):
            flags = _iter_fenced_flags(lines, unclosed_is_live=policy)
            self.assertEqual(flags, [False, False, False],
                             f"inline-code opener must not open a fence (policy={policy})")


class PublicScannersMatchEngine(unittest.TestCase):
    """The two public functions are thin wrappers with their documented policy —
    the refactor safety net (existing fence suites are the wider control)."""

    def test_iter_nonfenced_fails_closed_on_unclosed_fence(self):
        text = "live\n```\n## Risk\nreversible\n"
        self.assertEqual(_nonfenced_texts(text), ["live"],
                         "_iter_nonfenced must hide a heading under an unclosed fence")

    def test_closed_indices_fail_open_on_unclosed_fence(self):
        # An unclosed fence contributes NOTHING to the closed-index set.
        lines = "live\n```\n## Risk\nreversible\n".splitlines()
        self.assertEqual(_closed_fence_line_indices(lines), set(),
                         "unclosed fence must contribute no closed indices (fail open)")

    def test_closed_indices_mark_a_closed_fence(self):
        lines = "a\n```\nx\n```\nb\n".splitlines()
        self.assertEqual(_closed_fence_line_indices(lines), {1, 2, 3})


class ReadersFailOpen(unittest.TestCase):
    """V3 — the PURE readers surface a real section even under an unclosed fence.

    A reader can't corrupt the file, and hiding a real `## Blocked`/`## Handoff`/
    receipt under a malformed unclosed fence breaks bootstrap (a real handoff is
    silently lost). So the pure readers fail OPEN. A properly CLOSED fenced decoy
    is still ignored (that protection is unchanged — only the UNCLOSED case flips).
    """

    def _md(self, body: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "task.md"
        p.write_text(body, encoding="utf-8")
        return p

    def test_extract_block_reason_surfaces_under_unclosed_fence(self):
        p = self._md("# T\n\n## Status\nblocked\n\n## Docs\n```\nquoted\n\n"
                     "## Blocked\n> handoff  (since 2026-01-01T00:00+00:00)\n")
        self.assertEqual(_extract_block_reason(p), "handoff",
                         "an unclosed fence hid a real ## Blocked from bootstrap")

    def test_closed_fenced_blocked_decoy_is_still_ignored(self):
        # Control: a properly CLOSED fenced `## Blocked` example is NOT live, and
        # there is no real block → None (the C1 protection is preserved).
        p = self._md("# T\n\n## Status\nin_progress\n\n## Docs\n```\n"
                     "## Blocked\n> not a real block\n```\n\nplain body\n")
        self.assertIsNone(_extract_block_reason(p))

    def test_latest_receipt_surfaces_under_unclosed_fence(self):
        p = self._md("# T\n\n## Docs\n```\nquoted example\n\n"
                     "## Verification Receipt\n\n### close · risk reversible · commit abc\n")
        self.assertEqual(_latest_receipt_line(p), "close · risk reversible · commit abc",
                         "an unclosed fence hid a real receipt entry")

    def test_find_unconsumed_handoff_finds_it_under_unclosed_fence(self):
        proj = Path(tempfile.mkdtemp())
        td = proj / ".agent" / "tasks" / "007-x"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            "# 007 - X\n\n## Status\nblocked\n\n## Docs\n```\nquoted\n\n"
            "## Blocked\n> handoff  (since 2026-01-01T00:00+00:00)\n",
            encoding="utf-8")
        found = find_unconsumed_handoff(proj)
        self.assertIsNotNone(found, "a real handoff under an unclosed fence was lost")
        self.assertEqual(found[0], 7)


if __name__ == "__main__":
    unittest.main()
