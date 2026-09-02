#!/usr/bin/env python3
"""`## Risk: unclassified` must not be the cheapest way through the close gate.

Audit finding (1.5.31): `panel_required_for` keys the review bar to the risk
class, and `unclassified` is in no class — so the bar was never evaluated and the
task closed with a warning (lifecycle.py, "F14 Finding 3"). The rationale in the
code was backward compatibility: "every pre-1.5.0 task is unclassified". True 31
releases ago; today the cheapest path through the strictest gate in the system is
to leave one field blank — and the field is filled in by the same agent the gate
exists to constrain.

The discriminator is already on disk and needs no new metadata: pre-1.5.0
templates have NO `## Risk` section at all. So:

  * section ABSENT  → legacy task, the gate was never offered → warn and pass
                      (the 1.5.31 behavior, deliberately preserved);
  * section PRESENT but unset → the agent was offered the gate and skipped it
                      → treat as high-consequence and BLOCK without review
                      evidence, exactly like assertive/irreversible.

`--force --reason` remains the one blunt hatch (A8, unchanged).

Run: python3 tests/test_unclassified_fails_closed.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

from tasks.core import close_decision, extract_risk, has_risk_section  # noqa: E402

IMPL_EVIDENCE = (
    "# Panel Impl Review — task 1\n\n"
    "**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
    f"**Commit:** {'a' * 40}\n\nfindings body\n"
)


class UnclassifiedPolicy(unittest.TestCase):
    """The pure decision — tasks.core.close_decision."""

    def base(self, **kw):
        args = dict(risk="unclassified", verify_declared=False, verify_failed=False,
                    has_review_evidence=False, force=False, reason=None,
                    risk_section_present=True)
        args.update(kw)
        return close_decision(**args)

    def test_offered_but_unset_blocks(self):
        allowed, why = self.base()
        self.assertFalse(allowed, "an unset risk gate closed with no review evidence")
        self.assertIn("Risk", why)

    def test_the_block_names_the_one_line_fix(self):
        _, why = self.base()
        for token in ("reversible", "irreversible", "assertive"):
            self.assertIn(token, why, f"block message does not name {token}")

    def test_review_evidence_satisfies_it(self):
        """Same bar as assertive/irreversible — evidence, not classification,
        is what the gate is actually asking for."""
        allowed, why = self.base(has_review_evidence=True)
        self.assertTrue(allowed, why)

    def test_force_with_reason_still_passes(self):
        allowed, why = self.base(force=True, reason="owner accepted, legacy import")
        self.assertTrue(allowed, why)

    def test_force_without_reason_still_blocks_on_the_reason_rule(self):
        allowed, why = self.base(force=True)
        self.assertFalse(allowed)
        self.assertIn("--reason", why)

    def test_legacy_task_without_the_section_still_closes(self):
        """The backward-compat carve-out survives, narrowed to the tasks it was
        actually written for."""
        allowed, why = self.base(risk_section_present=False)
        self.assertTrue(allowed, why)

    def test_classified_risks_are_untouched(self):
        for risk in ("reversible",):
            with self.subTest(risk=risk):
                allowed, _ = self.base(risk=risk)
                self.assertTrue(allowed)
        for risk in ("assertive", "irreversible"):
            with self.subTest(risk=risk):
                allowed, _ = self.base(risk=risk)
                self.assertFalse(allowed, f"{risk} light-closed")
                allowed, _ = self.base(risk=risk, has_review_evidence=True)
                self.assertTrue(allowed)

    def test_default_is_the_lenient_legacy_path(self):
        """A caller that does not know whether the gate was offered must not
        have strictness invented for it — lifecycle passes the real value."""
        allowed, _ = close_decision(
            risk="unclassified", verify_declared=False, verify_failed=False,
            has_review_evidence=False, force=False, reason=None)
        self.assertTrue(allowed)


class RiskSectionDetection(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def _md(self, body: str) -> Path:
        p = self.d / "task.md"
        p.write_text(body, encoding="utf-8")
        return p

    def test_modern_template_has_the_section(self):
        p = self._md("# 001 - T\n\n## Status\npending\n\n## Risk\nunclassified\n")
        self.assertTrue(has_risk_section(p))

    def test_section_with_a_value_is_still_present(self):
        p = self._md("# 001 - T\n\n## Risk\nreversible\n")
        self.assertTrue(has_risk_section(p))

    def test_pre_150_template_does_not(self):
        p = self._md("# 001 - T\n\n## Status\npending\n\n## Work Plan\n- [x] G1\n")
        self.assertFalse(has_risk_section(p))

    # ── V8 (task 039 impl-panel): the risk path must use the strict shared
    # machinery, and PRESENCE must fail STRICT so a hidden/malformed `## Risk`
    # blocks rather than reading as pre-1.5.0 legacy-absent (lenient). ──────────

    def test_indented_sole_risk_after_blank_still_counts_as_offered(self):
        # opus CRITICAL: V4 marked the >=4-indent heading as code → hidden →
        # has_risk_section=False → lenient legacy close of an assertive task
        # (a regression V4 introduced). Presence must fail STRICT.
        p = self._md("# T\n\n## Docs\n\n    ## Risk\n    assertive\n")
        self.assertTrue(has_risk_section(p),
                        "an indented (hidden) ## Risk must still count as offered")
        self.assertEqual(extract_risk(p), "unclassified",
                         "an indented ## Risk is not a live classification (>=4 = code)")

    def test_indented_risk_no_blank_is_not_read_as_live(self):
        # codex-sol #1: `## Docs\n    ## Risk\n    reversible` read as reversible;
        # a >=4-indent heading is code per CommonMark, never a live value.
        p = self._md("# T\n\n## Docs\n    ## Risk\n    reversible\n")
        self.assertEqual(extract_risk(p), "unclassified")
        self.assertTrue(has_risk_section(p))

    def test_nbsp_led_risk_is_not_read_as_live_but_counts_as_offered(self):
        # codex-sol #2: a NBSP-led `## Risk` was read as a live reversible value
        # (NBSP-led is not an ATX heading). Value must not leak; presence blocks.
        p = self._md("# T\n\n ## Risk\nreversible\n")
        self.assertEqual(extract_risk(p), "unclassified")
        self.assertTrue(has_risk_section(p))

    def test_closing_hash_risk_heading_is_recognized(self):
        # codex-sol #3: `## Risk ##` (CommonMark closing sequence) is a valid H2;
        # it must be recognized so an assertive task is NOT legacy-lenient-closed.
        p = self._md("# T\n\n## Risk ##\nassertive\n")
        self.assertTrue(has_risk_section(p))
        self.assertEqual(extract_risk(p), "assertive")

    def test_nbsp_led_decoy_does_not_shadow_the_real_classification(self):
        # A NBSP-led decoy `reversible` must not turn the real col-0 `assertive`
        # into a duplicate-unclassified; the real class wins.
        p = self._md("# T\n\n ## Risk\nreversible\n\n## Risk\nassertive\n")
        self.assertEqual(extract_risk(p), "assertive")

    def test_a_mention_in_prose_is_not_a_section(self):
        """Negative control: the word must be a heading, not narrative — else
        every task that discusses risk gets silently promoted to strict."""
        p = self._md("# 001 - T\n\nThe ## Risk of this is low.\n### Risk notes\nfine\n")
        self.assertFalse(has_risk_section(p))

    def test_malformed_one_liner_counts_as_offered(self):
        """`## Risk: assertive` is not a valid classification (extract_risk
        degrades it to unclassified) — but the gate was plainly attempted, so it
        must land on the strict side, not be mistaken for a legacy task."""
        p = self._md("# 001 - T\n\n## Risk: assertive\n\n## Work Plan\n- [x] G1\n")
        self.assertTrue(has_risk_section(p))

    def test_light_templates_risk_routing_gate_is_not_the_field(self):
        """Negative control: the light template carries a `## Risk Routing` gate
        checklist as well. That is not the classification field, and treating it
        as one would strict-gate every legacy light task."""
        p = self._md("# 001 - T\n\n## Risk Routing\n- [x] set the risk\n")
        self.assertFalse(has_risk_section(p))

    def test_deeper_heading_is_not_the_field(self):
        p = self._md("# 001 - T\n\n### Risk\nassertive\n")
        self.assertFalse(has_risk_section(p))

    def test_bom_tabs_case_crlf_and_trailing_space_are_real_headings(self):
        cases = (
            "\ufeff## Risk\nunclassified\n",
            "##\tRisk\nunclassified\n",
            "## risk\nunclassified\n",
            "## Risk   \r\nunclassified\r\n",
        )
        for body in cases:
            with self.subTest(body=body):
                p = self._md(body)
                self.assertTrue(has_risk_section(p))
                self.assertEqual(extract_risk(p), "unclassified")

    def test_heading_inside_fence_is_not_metadata(self):
        for fence in ("```", "~~~~"):
            with self.subTest(fence=fence):
                p = self._md(
                    f"# Legacy\n\n{fence}md\n## Risk\nunclassified\n{fence}\n")
                self.assertFalse(has_risk_section(p))
                self.assertEqual(extract_risk(p), "unclassified")

    def test_duplicate_fields_fail_to_unclassified(self):
        for body in (
            "## Risk\n\n## Risk\nreversible\n",
            "## Risk\nreversible\n## Risk\n\n",
        ):
            with self.subTest(body=body):
                p = self._md(body)
                self.assertTrue(has_risk_section(p))
                self.assertEqual(extract_risk(p), "unclassified")

    def test_fenced_risk_decoy_cannot_shadow_the_real_classification(self):
        # Critical (panel codex-sol): a fenced `## Risk` example whose fence is
        # "closed" by a ```lang line (or a >=4-space-indented marker) must not be
        # read as live metadata. Otherwise the fenced `reversible` decoy shadows
        # the real `assertive`, and an assertive task closes on the lighter bar.
        # `_risk_heading_lines` must use the strict CommonMark fence rules.
        for interior in ("```yaml", "    ```"):
            with self.subTest(interior=interior):
                body = ("# T\n\n## Status\npending\n\n"
                        "## Docs\n```\nexample\n" + interior + "\n"
                        "## Risk\nreversible\n```\n\n"
                        "## Risk\nassertive\n\n## Work Plan\n- [ ] g\n")
                p = self._md(body)
                self.assertEqual(extract_risk(p), "assertive",
                                 "a fenced Risk decoy must not shadow the real class")

    def test_unclosed_fence_value_fails_closed_never_leaks_a_decoy(self):
        # Panel round-3: the risk VALUE must fail CLOSED on an unclosed fence — a
        # `## Risk` quoted after an unclosed opener is fenced-through-EOF, not live
        # metadata, so it can never BECOME the classification. (Fail-open reads the
        # decoy and returns `reversible`.) extract_risk stays fail-closed.
        p = self._md("# T\n\n## Docs\n```\n## Risk\nreversible\n> still quoted\n")
        self.assertEqual(extract_risk(p), "unclassified")

    def test_unclosed_fence_counts_as_offered_and_is_strict(self):
        # V2 (task 039, codex-sol #1 Critical): an unclosed fence makes the parse
        # UNCERTAIN. has_risk_section must fail toward STRICT ("offered") — True —
        # so the close is held to the high-consequence bar, not the lenient legacy
        # path. Previously this returned False, so burying the real `## Risk` under
        # an unclosed fence bought a pre-1.5.0 lenient close (the P-C bypass).
        # Value still fails closed (unclassified); present=True + unclassified =
        # BLOCK in close_decision.
        buried = self._md("# T\n\n## Docs\n```\n## Risk\nassertive\n> real class hidden\n")
        self.assertTrue(has_risk_section(buried),
                        "an unclosed fence must count as an offered (uncertain) risk gate")
        self.assertEqual(extract_risk(buried), "unclassified")
        # Negative control: a well-formed file with NO risk heading and no unclosed
        # fence is still legacy (present=False) — V2 does not over-block clean files.
        clean_legacy = self._md("# T\n\n## Docs\nplain paragraph, no fence, no risk\n")
        self.assertFalse(has_risk_section(clean_legacy))

    def test_unreadable_file_is_treated_as_legacy(self):
        """Fail toward the documented old behavior, never toward inventing a
        block from an I/O error."""
        self.assertFalse(has_risk_section(self.d / "does-not-exist.md"))

    def test_unclosed_fence_forces_unclassified_even_with_a_read_value(self):
        # V9/codex-terra CRITICAL (round-2 panel): an unclosed fence = UNCERTAIN
        # parse. `## Risk\nreversible` shown at top with `## Risk\nassertive` HIDDEN
        # under an unclosed fence would otherwise read reversible (the assertive
        # silently shadowed) and close on the low bar. extract_risk must degrade to
        # unclassified when the parse is uncertain → present+unclassified → BLOCK.
        p = self._md("# T\n\n## Risk\nreversible\n\n## Docs\n```\n## Risk\nassertive\n")
        self.assertEqual(extract_risk(p), "unclassified")
        self.assertTrue(has_risk_section(p))

    def test_stray_unclosed_fence_below_a_classified_risk_does_not_over_block(self):
        # V10/opus F1 (round-3 panel): V9 degraded on ANY unclosed fence, which
        # over-blocked a clean `reversible` whose long task.md merely has a stray
        # unbalanced ``` BELOW the risk (a receipt/notes snippet) that cannot hide
        # a `## Risk`. Refined: degrade ONLY when the unclosed-fence region hides a
        # `## Risk`-shape. A stray fence with no hidden risk keeps the real value.
        p = self._md("# T\n\n## Risk\nreversible\n\n## Notes\n```\nstray, no risk here\n")
        self.assertEqual(extract_risk(p), "reversible",
                         "a stray fence with no hidden ## Risk must not over-block")

    def test_balanced_fences_classified_risk_reads_its_class(self):
        # opus F3: a normal classified task with BALANCED fences reads its class.
        p = self._md("# T\n\n## Risk\nreversible\n\n## Docs\n```\nexample\n```\n")
        self.assertEqual(extract_risk(p), "reversible")


class CloseEndToEnd(unittest.TestCase):
    """Through the real CLI, because the wiring is the half that broke."""

    def _project(self, task_body: str) -> Path:
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        (d / "code.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=d, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "seed"], cwd=d, check=True)
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(task_body, encoding="utf-8")
        return d

    def _env(self):
        return dict(os.environ, PYTHONPATH=str(PLUGIN),
                    PLAYBOOK_SESSION_ID="pid-unclassified")

    def _cli(self, d: Path, *argv) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "tasks.cli", *argv],
                              cwd=d, env=self._env(), capture_output=True,
                              text=True, timeout=90)

    def _work_then_close(self, body: str, *close_args):
        d = self._project(body)
        r = self._cli(d, "work", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        return d, self._cli(d, "work", "done", *close_args)

    MODERN_UNSET = ("# 001 - T\n\n## Status\npending\n\n## Risk\nunclassified\n\n"
                    "## Work Plan\n- [x] G1: do it\n")
    LEGACY = ("# 001 - T\n\n## Status\npending\n\n"
              "## Work Plan\n- [x] G1: do it\n")
    CLASSIFIED = ("# 001 - T\n\n## Status\npending\n\n## Risk\nreversible\n\n"
                  "## Work Plan\n- [x] G1: do it\n")

    def test_modern_unset_risk_blocks_the_close(self):
        d, r = self._work_then_close(self.MODERN_UNSET)
        self.assertNotEqual(r.returncode, 0,
                            f"close succeeded on an unset risk gate:\n{r.stdout}")
        self.assertNotIn("Task 001 done.", r.stdout)
        # ...and nothing was recorded: a blocked close must change no state.
        body = (d / ".agent/tasks/001-t/task.md").read_text(encoding="utf-8")
        self.assertIn("pending", body)
        self.assertNotIn("## Status\ndone", body)

    def test_the_block_is_recoverable_by_classifying(self):
        body = self.MODERN_UNSET.replace("unclassified", "reversible")
        _, r = self._work_then_close(body)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Task 001 done.", r.stdout)

    def test_the_block_is_recoverable_by_forcing(self):
        _, r = self._work_then_close(self.MODERN_UNSET,
                                     "--force", "--reason", "legacy import")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Task 001 done.", r.stdout)

    def test_legacy_task_closes_with_the_warning(self):
        _, r = self._work_then_close(self.LEGACY)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Task 001 done.", r.stdout)
        self.assertIn("unclassified", r.stdout + r.stderr,
                      "legacy close went silent — it must still say what was skipped")

    def test_classified_reversible_is_unaffected(self):
        _, r = self._work_then_close(self.CLASSIFIED)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Task 001 done.", r.stdout)

    def test_bom_and_lowercase_unset_headings_block(self):
        for heading in ("\ufeff## Risk", "## risk"):
            with self.subTest(heading=heading):
                body = self.MODERN_UNSET.replace("## Risk", heading)
                _, r = self._work_then_close(body)
                self.assertNotEqual(r.returncode, 0)

    def test_fenced_risk_example_does_not_block_a_legacy_close(self):
        body = self.LEGACY.replace(
            "## Work Plan", "```md\n## Risk\nunclassified\n```\n\n## Work Plan")
        _, r = self._work_then_close(body)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unclosed_fence_hiding_the_real_risk_blocks_the_close(self):
        # V2 (task 039, codex-sol #1 Critical): burying the real `## Risk` under an
        # UNCLOSED fence used to make has_risk_section=False → the pre-1.5.0 lenient
        # legacy close, defeating the review requirement (P-C bypass). Now the
        # uncertain parse is strict: the close must BLOCK (no review evidence).
        body = ("# 001 - T\n\n## Status\npending\n\n"
                "## Docs\n```\n## Risk\nassertive\n> real class hidden below an "
                "unclosed fence\n\n## Work Plan\n- [x] G1: do it\n")
        d, r = self._work_then_close(body)
        self.assertNotEqual(r.returncode, 0,
                            f"an unclosed fence bought a lenient close:\n{r.stdout}")
        self.assertNotIn("Task 001 done.", r.stdout)
        # Recoverable the same way any strict block is: --force --reason.
        r2 = self._cli(d, "work", "done", "--force", "--reason", "malformed import")
        self.assertEqual(r2.returncode, 0, r2.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
