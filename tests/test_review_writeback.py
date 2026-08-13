#!/usr/bin/env python3
"""Single-judge reviews deliver findings via the trusted parent, not the judge.

The bug this locks down: `plan_review_prompt` used to instruct the judge to edit
`task.md`, while the launcher mounts the project read-only (`project_writable=
False`) AND snapshots `task.md` to detect tampering. The two are irreconcilable,
and which one bit you depended on the filesystem:

  * normal project path — containment holds, the judge's write fails with EROFS,
    and the findings are silently stranded in `judge-*.log`;
  * under /tmp — containment does not apply, the judge writes successfully, and
    `TAMPER DETECTED — Do NOT ingest this review` fires on a perfectly good
    review.

The fix keeps the judge read-only (that is a real security property — the guard
exists because a rogue judge once rewrote work-plan gates) and moves the write
to the parent, which is how `panel-review` already worked.

Two properties carry most of the risk and get the most attention here:

  1. **Idempotency.** Reviews are re-run constantly; a second run must replace
     the first run's findings, never append a second copy.
  2. **Untrusted input.** Findings are judge output. If they contain the
     sentinels this code uses as delimiters, a later rerun could bind to the
     wrong span and eat the surrounding gates — the very damage the tamper
     guard exists to prevent.

Pure stdlib unittest. Run: python3 tests/test_review_writeback.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from tasks import template  # noqa: E402
from tasks.cli import (  # noqa: E402
    _findings_markers,
    _neutralise_markers,
    _panel_triage_frame,
    _write_review_findings,
)


class NewTemplatePlaceholderAnchor(unittest.TestCase):
    """Gauntlet regression (1.5.2): the panel-first template renamed the section
    placeholder to '…triage appears here', and the single-judge FALLBACK's
    write-back only anchored on the old text — a live judge's findings could not
    land in a new-template task.md. Both generations must anchor."""

    def _tf(self, placeholder):
        import tempfile
        from pathlib import Path as _P
        tf = _P(tempfile.mkdtemp()) / "task.md"
        tf.write_text(f"# 1 - t\n\n## Implementation Review\n- [ ] gate\n\n{placeholder}\n\n---\n")
        return tf

    def test_old_placeholder_still_anchors(self):
        tf = self._tf("(implementation review findings appear here)")
        self.assertIsNone(_write_review_findings(tf, "impl", "finding"))
        self.assertIn("finding", tf.read_text())

    def test_new_triage_placeholder_anchors(self):
        tf = self._tf("(implementation review triage appears here)")
        self.assertIsNone(_write_review_findings(tf, "impl", "finding"))
        self.assertIn("finding", tf.read_text())


class PanelTriageFrameLimits(unittest.TestCase):
    """P11: the panel's own judge.md must name what a panel structurally cannot
    catch, so a clean panel is never read as 'all clear' on these classes."""

    def test_frame_names_the_three_limits(self):
        text = "\n".join(_panel_triage_frame()).lower()
        self.assertIn("correspondence", text)
        self.assertIn("disclosure", text)
        self.assertIn("irreversib", text)  # irreversibility / irreversible
        # and points each at its real (non-panel) check
        self.assertIn("screenshot", text)     # correspondence → the real artifact
        self.assertIn("risk", text)           # irreversibility → the ## Risk gate

TASK_MD = """# 007 - Demo

## Plan Review
- [ ] Run plan-review
- [ ] Triage findings

(plan review findings appear here)

---

## Work Plan

- [ ] gate that must survive
- [ ] second gate that must survive

---

## Implementation Review
- [ ] Run impl-review

(implementation review findings appear here)

---
"""


def fresh_task(text: str = TASK_MD) -> Path:
    d = Path(tempfile.mkdtemp())
    f = d / "task.md"
    f.write_text(text, encoding="utf-8")
    return f


class PromptContractTest(unittest.TestCase):
    """The prompts must not ask the judge for something it cannot do."""

    SINGLE = ("plan_review_prompt", "impl_review_prompt")
    PANEL = ("panel_plan_review_prompt", "panel_impl_review_prompt")

    def test_single_judge_prompts_no_longer_instruct_editing(self):
        for name in self.SINGLE:
            rendered = getattr(template, name)("a/007-demo/task.md")
            with self.subTest(builder=name):
                self.assertNotIn("Then edit", rendered)
                self.assertIn("DO NOT edit any files", rendered)
                self.assertIn("stdout only", rendered)

    def test_single_judge_prompts_no_longer_ask_for_gate_rewrites(self):
        """Rewriting the Work Plan was the judge's job and is now the agent's —
        the task template's own triage gate always said the agent decides what
        to accept, so a judge editing gates pre-empted that."""
        rendered = template.plan_review_prompt("a/007-demo/task.md")
        self.assertNotIn("revise the ## Work Plan gates", rendered)

    def test_panel_prompts_are_unchanged(self):
        """Guard against fixing the wrong pair: the panel path already worked."""
        for name in self.PANEL:
            rendered = getattr(template, name)("a/007-demo/task.md")
            with self.subTest(builder=name):
                self.assertIn(
                    "DO NOT edit any files. Output your findings to stdout only.",
                    rendered,
                )


class WriteBackTest(unittest.TestCase):
    def test_placeholder_is_replaced_and_surroundings_survive(self):
        f = fresh_task()
        self.assertIsNone(_write_review_findings(f, "plan", "1. **Critical** — boom"))
        text = f.read_text(encoding="utf-8")
        self.assertIn("1. **Critical** — boom", text)
        self.assertNotIn("(plan review findings appear here)", text)
        # The gates are the thing a bad replace would eat.
        self.assertIn("- [ ] gate that must survive", text)
        self.assertIn("- [ ] second gate that must survive", text)
        self.assertIn("- [ ] Triage findings", text)
        # The other section is untouched.
        self.assertIn("(implementation review findings appear here)", text)

    def test_impl_mode_targets_its_own_section(self):
        f = fresh_task()
        self.assertIsNone(_write_review_findings(f, "impl", "impl finding"))
        text = f.read_text(encoding="utf-8")
        self.assertIn("impl finding", text)
        self.assertIn("(plan review findings appear here)", text)

    def test_both_sections_can_hold_findings_independently(self):
        f = fresh_task()
        _write_review_findings(f, "plan", "PLAN-A")
        _write_review_findings(f, "impl", "IMPL-A")
        text = f.read_text(encoding="utf-8")
        self.assertIn("PLAN-A", text)
        self.assertIn("IMPL-A", text)

    def test_rerun_replaces_rather_than_appends(self):
        """The property that actually bites: reviews get re-run."""
        cases = [
            ("same findings twice", "SAME", "SAME"),
            ("different findings", "FIRST", "SECOND"),
            ("shorter second run", "a long first set of findings", "tiny"),
            ("findings with markdown", "- [ ] looks like a gate", "plain"),
        ]
        for label, first, second in cases:
            with self.subTest(case=label):
                f = fresh_task()
                _write_review_findings(f, "plan", first)
                after_first = f.read_text(encoding="utf-8")
                _write_review_findings(f, "plan", second)
                after_second = f.read_text(encoding="utf-8")

                open_m, close_m = _findings_markers("plan")
                self.assertEqual(after_second.count(open_m), 1)
                self.assertEqual(after_second.count(close_m), 1)
                self.assertIn(second, after_second)
                if first != second:
                    self.assertNotIn(first, after_second)
                else:
                    self.assertEqual(after_first, after_second)
                # Gates survive every rerun.
                self.assertIn("- [ ] gate that must survive", after_second)

    def test_writing_the_same_findings_is_byte_identical(self):
        f = fresh_task()
        _write_review_findings(f, "plan", "stable findings")
        once = f.read_bytes()
        for _ in range(3):
            _write_review_findings(f, "plan", "stable findings")
        self.assertEqual(once, f.read_bytes())


class UntrustedFindingsTest(unittest.TestCase):
    """Findings are judge output — treat them as hostile text."""

    def test_findings_cannot_smuggle_the_closing_sentinel(self):
        open_m, close_m = _findings_markers("plan")
        f = fresh_task()
        evil = f"legit finding {close_m} trailing text that must not escape"
        self.assertIsNone(_write_review_findings(f, "plan", evil))
        text = f.read_text(encoding="utf-8")
        # Exactly one real delimiter pair, despite the smuggled one.
        self.assertEqual(text.count(open_m), 1)
        self.assertEqual(text.count(close_m), 1)

    def test_a_rerun_after_smuggling_still_binds_correctly(self):
        """The real damage would land on the NEXT run, not this one."""
        _, close_m = _findings_markers("plan")
        f = fresh_task()
        _write_review_findings(f, "plan", f"evil {close_m} tail")
        self.assertIsNone(_write_review_findings(f, "plan", "clean second run"))
        text = f.read_text(encoding="utf-8")
        self.assertIn("clean second run", text)
        self.assertNotIn("evil", text)
        self.assertIn("- [ ] gate that must survive", text)

    def test_neutralise_is_reversible_looking_but_not_a_delimiter(self):
        open_m, close_m = _findings_markers("plan")
        out = _neutralise_markers(f"{open_m} x {close_m}", "plan")
        self.assertNotIn(open_m, out)
        self.assertNotIn(close_m, out)
        self.assertIn("x", out)


class RefusalTest(unittest.TestCase):
    """Refuse rather than guess: the findings survive in the judge log, but a
    wrong insertion could destroy work-plan gates."""

    def test_refuses_when_section_was_hand_edited(self):
        f = fresh_task(TASK_MD.replace(
            "(plan review findings appear here)", "my own hand-written notes"))
        before = f.read_bytes()
        reason = _write_review_findings(f, "plan", "X")
        self.assertIsNotNone(reason)
        self.assertIn("neither its placeholder nor findings markers", reason)
        self.assertEqual(before, f.read_bytes())

    def test_refuses_on_duplicate_marker_pairs_inside_the_section(self):
        """Duplicates must be judged within the section, which is where they
        would actually make the replace ambiguous."""
        open_m, close_m = _findings_markers("plan")
        dup = f"{open_m}\nstale\n{close_m}"
        f = fresh_task(TASK_MD.replace(
            "(plan review findings appear here)", f"{dup}\n\n{dup}"))
        before = f.read_bytes()
        reason = _write_review_findings(f, "plan", "two")
        self.assertIsNotNone(reason)
        self.assertIn("expected exactly one of each", reason)
        self.assertEqual(before, f.read_bytes())

    def test_markers_outside_the_section_are_ignored(self):
        """A marker quoted elsewhere in the file (prose, an example, another
        section) must not capture the write — that was a real defect: the search
        used to span the whole file."""
        open_m, close_m = _findings_markers("plan")
        f = fresh_task(TASK_MD.replace(
            "## Work Plan",
            f"## Work Plan\n\nDocs example: {open_m} sample {close_m}\n"))
        self.assertIsNone(_write_review_findings(f, "plan", "real findings"))
        text = f.read_text(encoding="utf-8")
        # Landed in the Plan Review section, not at the quoted example.
        plan_sec = text[text.index("## Plan Review"):text.index("## Work Plan")]
        self.assertIn("real findings", plan_sec)
        self.assertIn("Docs example:", text)
        self.assertIn("- [ ] gate that must survive", text)

    def test_refuses_on_reversed_markers(self):
        open_m, close_m = _findings_markers("plan")
        f = fresh_task(TASK_MD.replace(
            "(plan review findings appear here)", f"{close_m}\nstuff\n{open_m}"))
        before = f.read_bytes()
        reason = _write_review_findings(f, "plan", "X")
        self.assertIsNotNone(reason)
        self.assertIn("out of order", reason)
        self.assertEqual(before, f.read_bytes())

    def test_refuses_on_duplicate_placeholder(self):
        f = fresh_task(TASK_MD.replace(
            "## Work Plan", "(plan review findings appear here)\n\n## Work Plan"))
        before = f.read_bytes()
        reason = _write_review_findings(f, "plan", "X")
        self.assertIsNotNone(reason)
        self.assertIn("more than once", reason)
        self.assertEqual(before, f.read_bytes())

    def test_refuses_unknown_review_mode(self):
        f = fresh_task()
        before = f.read_bytes()
        self.assertIsNotNone(_write_review_findings(f, "sideways", "X"))
        self.assertEqual(before, f.read_bytes())

    def test_leaves_no_temp_files_behind(self):
        f = fresh_task()
        _write_review_findings(f, "plan", "findings")
        _write_review_findings(f, "plan", "more findings")
        self.assertEqual(list(f.parent.glob("*.tmp.*")), [])


class AtomicityTest(unittest.TestCase):
    def test_write_is_atomic_via_replace(self):
        """task.md IS the execution trace; an interrupt must not truncate it.

        Simulated by making os.replace fail: the original must be intact and no
        debris left, rather than a half-written file.
        """
        f = fresh_task()
        _write_review_findings(f, "plan", "good findings")
        intact = f.read_bytes()

        import tasks.cli as cli_mod
        orig_replace = cli_mod.os.replace

        def boom(src, dst):
            raise OSError("simulated interrupt")

        cli_mod.os.replace = boom
        try:
            reason = _write_review_findings(f, "plan", "should not land")
        finally:
            cli_mod.os.replace = orig_replace

        self.assertIsNotNone(reason)
        self.assertEqual(intact, f.read_bytes())
        self.assertEqual(list(f.parent.glob("*.tmp.*")), [])


if __name__ == "__main__":
    unittest.main()
