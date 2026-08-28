#!/usr/bin/env python3
"""Reviews advise a background launch when their hard timeout can outlast the
600 s foreground tool-call cap, and the skill/CLAUDE.md template say so (task 038).

A foreground `tasks panel-review` / `impl-review` / `plan-review` that runs past
the cap is killed mid-run; the advisory is what steers an agent to launch it
detached instead.

Run: python3 -m unittest tests.test_review_background_advisory
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks import review  # noqa: E402

_PLUGIN = _HERE.parent / "plugins/playbook"


class BackgroundAdvisoryText(unittest.TestCase):
    def _emit(self, hard) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            review._print_background_advisory(hard)
        return buf.getvalue()

    def test_prints_over_the_cap(self):
        out = self._emit(1200)
        self.assertIn("600 s foreground tool-call cap", out)
        self.assertIn("background", out.lower())

    def test_prints_when_unlimited(self):
        """None = no hard kill = certainly can exceed the cap."""
        self.assertIn("600 s foreground tool-call cap", self._emit(None))

    def test_silent_at_or_under_the_cap(self):
        self.assertEqual(self._emit(600), "",
                         "600 is not > 600 — a bounded run at the cap must be silent")
        self.assertEqual(self._emit(120), "")

    def test_cap_constant_is_600(self):
        self.assertEqual(review._FOREGROUND_TOOL_CAP_SECS, 600)


class ReviewsCallTheAdvisory(unittest.TestCase):
    """Pin that both review entrypoints actually invoke the advisory — a source
    check so a refactor that drops the call is caught."""

    def test_panel_and_single_review_call_the_advisory(self):
        src = (_PLUGIN / "tasks" / "review.py").read_text(encoding="utf-8")
        self.assertEqual(
            src.count("_print_background_advisory("), 3,
            "expected 3 references: the def + the panel call + the single-review "
            "call (covers panel/impl/plan)")


class DocsInstructBackgroundRuns(unittest.TestCase):
    """The skill + CLAUDE.md template + task template must tell agents to run
    reviews in the background."""

    def _read(self, rel: str) -> str:
        return (_PLUGIN / rel).read_text(encoding="utf-8").lower()

    def test_claude_md_template_instructs_background(self):
        t = self._read("scripts/CLAUDE.md.template")
        self.assertIn("background", t)
        self.assertIn("600 s foreground tool-call cap", t)

    def test_playbook_skill_instructs_background(self):
        self.assertIn("600 s foreground tool-call cap",
                      self._read("skills/playbook/SKILL.md"))

    def test_task_template_review_gates_say_background(self):
        t = self._read("skills/tasks/base-template.md")
        # Both the plan-review and impl-review gate lines carry the instruction.
        self.assertGreaterEqual(t.count("launch it in the background"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
