#!/usr/bin/env python3
"""Point tests for `select_task_context` — structure-aware, receipted truncation.

The bug this pins (report C3 / P3): a task.md is append-ordered, so the old
`content[:budget]` head-slice kept Intent + Design and dropped the CURRENT
round's fixes first — a judge reviewed the design four rounds running and never
saw a fix. The replacement must keep BOTH ends (orientation always, most-recent
sections next) and must NEVER truncate silently — every drop is named in a
receipt so the operator can compensate.

Invariants:
  * content within budget → returned verbatim, empty receipt (no false drop);
  * orientation sections (Intent/Design/Handoff) survive even when old and large;
  * the MOST RECENT section survives when the budget allows only some sections
    (the exact case the head-slice failed);
  * the receipt names every dropped section (no silent loss);
  * output never exceeds the budget by more than the safety marker (argv guard).

Pure stdlib unittest. Run: python3 tests/test_select_task_context.py
"""
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.core import select_task_context, split_md_sections  # noqa: E402


def section(heading: str, chars: int) -> str:
    body = f"body-of-{heading.replace(' ', '-')} " * ((chars // 24) + 1)
    return f"## {heading}\n\n{body[:chars]}\n\n"


class SplitSections(unittest.TestCase):
    def test_preamble_becomes_headless_section(self):
        text = "# Task 12 — thing\n\nlede\n\n" + section("Intent", 40)
        secs = split_md_sections(text)
        self.assertEqual(secs[0][0], "Task 12 — thing")  # level-1 heading
        self.assertTrue(any(h == "Intent" for h, _ in secs))

    def test_level3_stays_inside_parent(self):
        text = "## Design\n\ntop\n\n### sub A\n\ndetail\n"
        secs = split_md_sections(text)
        self.assertEqual(len(secs), 1)
        self.assertIn("### sub A", secs[0][1])

    def test_reassembly_is_lossless(self):
        text = section("Intent", 100) + section("Design", 100) + section("Debrief", 100)
        self.assertEqual("".join(c for _, c in split_md_sections(text)), text)


class SelectTaskContext(unittest.TestCase):
    def test_within_budget_verbatim_and_no_receipt(self):
        text = section("Intent", 200) + section("Work", 200)
        out, receipt = select_task_context(text, 10_000)
        self.assertEqual(out, text)
        self.assertEqual(receipt, "")

    def test_recent_section_survives_when_old_middle_dropped(self):
        # Intent(orientation) + three big middle sections + a small recent one.
        # Budget fits orientation + the recent tail but not the fat middle.
        text = (
            section("Intent", 500)
            + section("Work Plan", 8000)
            + section("Plan Review", 8000)
            + section("Implementation Review", 8000)
            + section("Debrief", 500)
        )
        out, receipt = select_task_context(text, 3000)
        self.assertIn("## Intent", out)          # orientation kept
        self.assertIn("## Debrief", out)         # most-recent kept — the head-slice failure
        self.assertNotIn("body-of-Plan-Review", out)  # fat middle dropped
        self.assertIn("Plan Review", receipt)    # and named in the receipt
        self.assertIn("dropped:", receipt)

    def test_orientation_survives_even_when_old_and_large(self):
        text = (
            section("Intent", 6000)
            + section("Design", 6000)
            + section("Work Plan", 6000)
            + section("Debrief", 200)
        )
        out, _ = select_task_context(text, 7000)
        self.assertIn("## Intent", out)
        self.assertIn("## Design", out)

    def test_handoff_is_orientation(self):
        text = section("Work Plan", 8000) + section("Handoff", 300) + section("Notes", 8000)
        out, _ = select_task_context(text, 2000)
        self.assertIn("## Handoff", out)

    def test_receipt_reports_char_counts(self):
        text = section("Intent", 200) + section("Big", 20_000)
        _, receipt = select_task_context(text, 5000)
        self.assertIn("task.md", receipt)
        self.assertIn("→", receipt)
        self.assertIn("kept:", receipt)

    def test_never_silent_every_drop_named(self):
        text = section("Intent", 200) + section("A", 9000) + section("B", 9000)
        out, receipt = select_task_context(text, 2000)
        for _, chunk in split_md_sections(text):
            pass
        # Anything not present in the output must appear in the receipt's drop list.
        if "body-of-A" not in out:
            self.assertIn("A", receipt)
        if "body-of-B" not in out:
            self.assertIn("B", receipt)

    def test_output_bounded_even_when_orientation_overflows(self):
        # Single orientation section far larger than the budget: kept, but bounded.
        text = section("Design", 50_000)
        out, receipt = select_task_context(text, 5000)
        self.assertLessEqual(len(out), 5000 + 80)  # budget + short marker
        self.assertIn("hard-truncated", receipt)


if __name__ == "__main__":
    unittest.main()
