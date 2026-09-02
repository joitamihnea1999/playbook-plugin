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
    _atx_h2_text,
    _closed_fence_line_indices,
    _extract_block_reason,
    _iter_fenced_flags,
    _iter_nonfenced,
    _latest_receipt_line,
    _live_section_span,
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


class IndentedCodeBlocks(unittest.TestCase):
    """V4 — a heading inside a bare indented code block (>=4 columns, no fence,
    after a blank line) is content, not a live section (codex-sol #2 Critical,
    P-B). CommonMark: an indented code block starts on a >=4-column line after a
    blank line and cannot interrupt a paragraph."""

    def _nf(self, text: str) -> "list[str]":
        return _nonfenced_texts(text)

    def test_four_space_indented_heading_after_blank_is_code(self):
        text = "## Docs\n\n    ## Risk\n    reversible\n"
        self.assertNotIn("## Risk", self._nf(text),
                         "a 4-space-indented ## Risk after a blank must be code")

    def test_tab_indented_heading_after_blank_is_code(self):
        text = "## Docs\n\n\t## Risk\n\treversible\n"
        self.assertNotIn("## Risk", self._nf(text),
                         "a tab-indented ## Risk (tab=4 cols) must be code")

    def test_heading_deep_in_a_multiline_indented_block_is_code(self):
        # The heading is NOT the first line of the block — proper state tracking,
        # not just "line after a blank".
        text = "## Docs\n\n    some code\n    ## Risk\n    reversible\n"
        self.assertNotIn("## Risk", self._nf(text),
                         "a ## Risk inside a running indented block must be code")

    def test_three_space_indent_is_still_a_live_heading(self):
        # Control: <4 columns is NOT an indented code block.
        text = "## Docs\n\n   ## Risk\nreversible\n"
        self.assertIn("## Risk", self._nf(text),
                      "a 3-space indent must remain a live heading")

    def test_indented_line_continuing_a_paragraph_is_not_code(self):
        # Control: an indented code block cannot INTERRUPT a paragraph (no blank
        # line before the indent) — so a real column-0 heading right after stays
        # a live boundary and the paragraph's own indented tail is not spuriously
        # treated as opening code that swallows it.
        text = "a paragraph line\n    ## Risk\n\n## Work Plan\n"
        nf = self._nf(text)
        self.assertIn("## Work Plan", nf,
                      "a real heading after a paragraph must stay live")

    def test_indented_risk_decoy_does_not_become_the_classification(self):
        from tasks.core import extract_risk, has_risk_section
        d = Path(tempfile.mkdtemp())
        p = d / "task.md"
        # Indented decoy `reversible` + a real column-0 `assertive`.
        p.write_text("# T\n\n## Docs\n\n    ## Risk\n    reversible\n\n"
                     "## Risk\nassertive\n\n## Work Plan\n- [ ] g\n", encoding="utf-8")
        self.assertEqual(extract_risk(p), "assertive",
                         "an indented ## Risk decoy must not shadow the real class")
        self.assertTrue(has_risk_section(p))


class NbspFenceCloser(unittest.TestCase):
    """V6 — a fence closer's tail must be ASCII whitespace only. `str.strip()`
    eats U+00A0/other Unicode whitespace, so a ```+NBSP line falsely CLOSED a
    fence and exposed an interior `## Risk` decoy as live (P-A). A tab after the
    run still closes (tab is valid ASCII whitespace)."""

    def _md(self, body: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "task.md"
        p.write_text(body, encoding="utf-8")
        return p

    def test_nbsp_after_the_run_does_not_close_the_fence(self):
        lines = ["## Docs", "```", "hidden", "``` ", "## Risk", "reversible"]
        flags = _iter_fenced_flags(lines, unclosed_is_live=False)
        self.assertTrue(flags[4] and flags[5],
                        "a ```+NBSP must NOT close the fence and expose the decoy")

    def test_tab_after_the_run_still_closes(self):
        # Control: a tab is ASCII whitespace, so ```+tab is a real closer.
        lines = ["```", "x", "```\t", "## Risk"]
        flags = _iter_fenced_flags(lines, unclosed_is_live=False)
        self.assertFalse(flags[3], "```+tab is a valid closer; the heading is live")

    def test_nbsp_false_closer_does_not_leak_a_risk_decoy(self):
        from tasks.core import extract_risk
        p = self._md("# T\n\n## Docs\n```\nhidden\n``` \n## Risk\nreversible\n")
        self.assertEqual(extract_risk(p), "unclassified",
                         "an NBSP false-closer leaked a reversible decoy")


class StrictAtxH2Matcher(unittest.TestCase):
    """V5 — one strict ATX-H2 matcher (<=3 leading spaces; `## ` OR `##\\t`), so
    the section WRITERS and READERS agree on what a boundary is (P-D)."""

    def test_space_separator_matches(self):
        self.assertEqual(_atx_h2_text("## Blocked"), "## Blocked")

    def test_up_to_three_leading_spaces_matches(self):
        self.assertEqual(_atx_h2_text("   ## Blocked"), "## Blocked")

    def test_tab_after_hash_is_a_boundary(self):
        # `##\tX` was missed by startswith("## ") — it must normalize to `## X`.
        self.assertEqual(_atx_h2_text("##\tBlocked"), "## Blocked")

    def test_four_leading_spaces_is_not_a_heading(self):
        self.assertIsNone(_atx_h2_text("    ## Blocked"))

    def test_h3_is_not_an_h2(self):
        self.assertIsNone(_atx_h2_text("### Entry"))

    def test_no_separator_is_not_an_h2(self):
        self.assertIsNone(_atx_h2_text("##Blocked"))

    def test_bom_prefix_is_tolerated(self):
        self.assertEqual(_atx_h2_text("﻿## Blocked"), "## Blocked")

    def test_live_section_span_finds_a_tab_separated_title(self):
        lines = ["# T", "", "##\tBlocked", "> reason", "", "## Work Plan", "- [ ] g"]
        span = _live_section_span(lines, "## Blocked")
        self.assertIsNotNone(span, "a tab-after-hash ## Blocked must be locatable")
        self.assertEqual(span, (2, 5))

    def test_live_section_span_boundary_stops_at_tab_heading(self):
        # The section must END at a `##\tNext` boundary, not run through it.
        lines = ["## Blocked", "> r", "##\tNext", "body"]
        self.assertEqual(_live_section_span(lines, "## Blocked"), (0, 2))


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
