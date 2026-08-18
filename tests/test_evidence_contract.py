#!/usr/bin/env python3
"""Point tests for the evidence contract + consequence classification (P1 / P2).

Pins the close policy that turns `tasks work done` from "write the string done"
into an earned close:
  * a forced close ALWAYS needs a reason (046 left no trace);
  * a failing declared verify blocks unless forced;
  * an assertive/irreversible task with no review evidence cannot light-close
    (the 056 inversion: a docs-only diff made the biggest false claim and got a
    light close);
  * the verify contract resolves by risk class with an `_always` base bar and a
    legacy merge_verify fallback;
  * risk + review-evidence are read mechanically from task.md / the task dir.

Pure stdlib unittest. Run: python3 tests/test_evidence_contract.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.core import (  # noqa: E402
    close_decision, extract_risk, format_verify_receipt, has_review_evidence,
    resolve_verify_commands,
)


class CloseDecision(unittest.TestCase):
    def base(self, **kw):
        args = dict(risk="reversible", verify_declared=False, verify_failed=False,
                    has_review_evidence=False, force=False, reason=None)
        args.update(kw)
        return close_decision(**args)

    def test_clean_reversible_closes(self):
        allowed, why = self.base()
        self.assertTrue(allowed)
        self.assertEqual(why, "")

    def test_force_without_reason_blocks(self):
        allowed, why = self.base(force=True)
        self.assertFalse(allowed)
        self.assertIn("--reason", why)

    def test_force_with_reason_allows(self):
        allowed, _ = self.base(force=True, reason="owner accepts, seat 401")
        self.assertTrue(allowed)

    def test_failing_verify_blocks(self):
        allowed, why = self.base(verify_declared=True, verify_failed=True)
        self.assertFalse(allowed)
        self.assertIn("verification failed", why)

    def test_failing_verify_forced_allows(self):
        allowed, _ = self.base(verify_declared=True, verify_failed=True,
                               force=True, reason="hotfix, tests flaky")
        self.assertTrue(allowed)

    def test_assertive_without_review_blocks(self):
        allowed, why = self.base(risk="assertive")
        self.assertFalse(allowed)
        self.assertIn("assertive", why)

    def test_assertive_with_review_closes(self):
        allowed, _ = self.base(risk="assertive", has_review_evidence=True)
        self.assertTrue(allowed)

    def test_irreversible_without_review_blocks(self):
        allowed, _ = self.base(risk="irreversible")
        self.assertFalse(allowed)

    def test_undeclared_verify_does_not_block(self):
        # user's choice: no contract → warn+allow, never block on verify.
        allowed, _ = self.base(verify_declared=False, verify_failed=False)
        self.assertTrue(allowed)


class ExtractRisk(unittest.TestCase):
    def _task(self, body):
        d = tempfile.mkdtemp()
        p = Path(d) / "task.md"
        p.write_text(body, encoding="utf-8")
        return p

    def test_reads_valid_class(self):
        p = self._task("# 1 - t\n\n## Risk\nassertive\n\n## Intent\nx\n")
        self.assertEqual(extract_risk(p), "assertive")

    def test_strips_markup(self):
        p = self._task("## Risk\n`irreversible`\n")
        self.assertEqual(extract_risk(p), "irreversible")

    def test_absent_is_unclassified(self):
        p = self._task("# 1 - t\n\n## Intent\nx\n")
        self.assertEqual(extract_risk(p), "unclassified")

    def test_garbage_is_unclassified(self):
        p = self._task("## Risk\nmaybe-dangerous\n")
        self.assertEqual(extract_risk(p), "unclassified")


class ReviewEvidence(unittest.TestCase):
    def _dir(self, judge_md=None, task_md="# 1 - t\n"):
        d = Path(tempfile.mkdtemp())
        if judge_md is not None:
            (d / "judge.md").write_text(judge_md)
        (d / "task.md").write_text(task_md)
        return d / "task.md"

    def test_judge_md_present(self):
        self.assertTrue(has_review_evidence(self._dir(judge_md="# panel\n")))

    def test_checked_impl_review_gate(self):
        tf = self._dir(task_md="## Implementation Review\n- [x] Run `.claude/bin/tasks impl-review 1`\n")
        self.assertTrue(has_review_evidence(tf))
        self.assertTrue(has_review_evidence(tf, impl_only=True))

    def test_unchecked_gate_is_no_evidence(self):
        tf = self._dir(task_md="## Implementation Review\n- [ ] Run `.claude/bin/tasks impl-review 1`\n")
        self.assertFalse(has_review_evidence(tf))

    def test_plan_evidence_does_not_satisfy_impl_only(self):
        """A plan review examines intent before the work exists — it must not
        satisfy the high-consequence close gate (A4)."""
        plan_judge = self._dir(judge_md="# Panel Plan Review — task\n")
        self.assertTrue(has_review_evidence(plan_judge))            # counts as A review
        self.assertFalse(has_review_evidence(plan_judge, impl_only=True))
        plan_gate = self._dir(task_md="## Plan Review\n- [x] Run `.claude/bin/tasks plan-review 1`\n")
        self.assertFalse(has_review_evidence(plan_gate, impl_only=True))

    def test_impl_judge_md_satisfies_impl_only(self):
        tf = self._dir(judge_md="# Panel Impl Review — task\n\n**PANEL VERDICT: PASS**\n")
        self.assertTrue(has_review_evidence(tf, impl_only=True))

    def test_panel_gate_with_impl_mode_satisfies_impl_only(self):
        tf = self._dir(task_md="- [x] Run `.claude/bin/tasks panel-review 1 --mode impl`\n")
        self.assertTrue(has_review_evidence(tf, impl_only=True))


class ResolveVerifyCommands(unittest.TestCase):
    def _proj(self, cfg):
        d = Path(tempfile.mkdtemp())
        agent = d / ".agent"
        agent.mkdir()
        if cfg is not None:
            (agent / "config.json").write_text(json.dumps(cfg))
        return d

    def test_string_form_always_runs(self):
        p = self._proj({"verify": "npm test"})
        self.assertEqual(resolve_verify_commands(p, "reversible"),
                         [("verify", "npm test")])

    def test_always_plus_risk_keyed(self):
        p = self._proj({"verify": {"_always": ["check"], "assertive": ["check:claims"]}})
        got = resolve_verify_commands(p, "assertive")
        self.assertEqual(got, [("verify._always", "check"),
                               ("verify.assertive", "check:claims")])
        # a reversible task only gets the base bar
        self.assertEqual(resolve_verify_commands(p, "reversible"),
                         [("verify._always", "check")])

    def test_legacy_merge_verify_fallback(self):
        p = self._proj({"merge_verify": {"command": "make ci"}})
        self.assertEqual(resolve_verify_commands(p, "reversible"),
                         [("merge_verify.command", "make ci")])

    def test_nothing_declared(self):
        p = self._proj({})
        self.assertEqual(resolve_verify_commands(p, "reversible"), [])


class PanelAlwaysPolicy(unittest.TestCase):
    """Owner policy `panel_required_for` (StrataDB owner's call: 'another pair of
    eyes is always better — enforce it'): the configured risk classes (or "all")
    demand PANEL-grade impl evidence at close — quorum-PASS panel impl review —
    not just a single judge."""

    def _proj(self, cfg):
        d = Path(tempfile.mkdtemp())
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(json.dumps(cfg))
        return d

    def test_resolve_all_and_list_and_absent(self):
        from tasks.core import resolve_panel_required
        self.assertTrue(resolve_panel_required(self._proj({"panel_required_for": "all"}), "reversible"))
        lst = self._proj({"panel_required_for": ["irreversible"]})
        self.assertTrue(resolve_panel_required(lst, "irreversible"))
        self.assertFalse(resolve_panel_required(lst, "reversible"))
        self.assertFalse(resolve_panel_required(self._proj({}), "irreversible"))
        self.assertFalse(resolve_panel_required(self._proj({"panel_required_for": 7}), "assertive"))

    def test_panel_evidence_requires_impl_mode_and_pass(self):
        from tasks.core import has_panel_impl_evidence
        d = Path(tempfile.mkdtemp())
        (d / "task.md").write_text("# 1 - t\n")
        tf = d / "task.md"
        self.assertFalse(has_panel_impl_evidence(tf))                       # no judge.md
        (d / "judge.md").write_text("# Panel Plan Review — t\n\n**PANEL VERDICT: PASS** — ok\n")
        self.assertFalse(has_panel_impl_evidence(tf), "plan panel can't vouch for the build")
        (d / "judge.md").write_text("# Panel Impl Review — t\n\n**PANEL VERDICT: FAIL** — 1/4, quorum 3\n")
        self.assertFalse(has_panel_impl_evidence(tf), "a degraded panel is not a panel")
        (d / "judge.md").write_text("# Panel Impl Review — t\n\n**PANEL VERDICT: PASS** — 4/4, quorum 3\n")
        self.assertTrue(has_panel_impl_evidence(tf))

    def test_close_decision_blocks_without_panel_evidence(self):
        allowed, why = close_decision(
            risk="reversible", verify_declared=False, verify_failed=False,
            has_review_evidence=False, force=False, reason=None, panel_required=True)
        self.assertFalse(allowed)
        self.assertIn("panel", why.lower())
        allowed, _ = close_decision(
            risk="reversible", verify_declared=False, verify_failed=False,
            has_review_evidence=True, force=False, reason=None, panel_required=True)
        self.assertTrue(allowed)


class Receipt(unittest.TestCase):
    def test_records_pass_and_fail(self):
        r = format_verify_receipt(
            [("verify._always", "npm test", 0, "42 passing\n"),
             ("verify.assertive", "check:claims", 1, "AssertionError: drift\n")],
            head_sha="abc1234", risk="assertive", timestamp="2026-08-11T10:00:00")
        self.assertIn("[PASS] `npm test`", r)
        self.assertIn("[FAIL(exit 1)] `check:claims`", r)
        self.assertIn("abc1234", r)
        self.assertIn("assertive", r)

    def test_no_commands_says_nothing_verified(self):
        r = format_verify_receipt([], head_sha="abc", risk="reversible",
                                  timestamp="2026-08-11T10:00:00")
        self.assertIn("NONE DECLARED", r)

    def test_forced_reason_recorded(self):
        r = format_verify_receipt([], head_sha="abc", risk="reversible",
                                  reason="owner override", timestamp="t")
        self.assertIn("owner override", r)

    def test_dirty_tree_marker(self):
        """StrataDB F6: close-then-commit is the normal flow, so a receipt whose
        stamped commit predates the verified code must say so."""
        dirty = format_verify_receipt([], head_sha="abc", risk="reversible",
                                      timestamp="t", dirty_files=7)
        self.assertIn("+7 uncommitted file(s)", dirty)
        clean = format_verify_receipt([], head_sha="abc", risk="reversible", timestamp="t")
        self.assertNotIn("uncommitted", clean)

    def test_entry_form_no_own_heading(self):
        """The heading belongs to upsert_task_section — the entry leads with a
        `###` stamp so re-closes stack under ONE section, newest first (A3)."""
        r = format_verify_receipt([], head_sha="abc", risk="reversible", timestamp="t")
        self.assertTrue(r.startswith("### t · risk reversible · commit abc"), r)
        self.assertNotIn("## Verification Receipt", r)


class UpsertAndAtomicWrite(unittest.TestCase):
    def test_upsert_keeps_one_heading_newest_first(self):
        from tasks.core import upsert_task_section
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text("# 1 - t\n\n## Status\ndone\n", encoding="utf-8")
        upsert_task_section(tf, "Verification Receipt", "### first close\n- a\n")
        upsert_task_section(tf, "Verification Receipt", "### second close\n- b\n")
        text = tf.read_text(encoding="utf-8")
        self.assertEqual(text.count("## Verification Receipt"), 1)
        self.assertEqual(text.count("### "), 2)
        self.assertLess(text.index("### second close"), text.index("### first close"),
                        "newest entry must come first — the first thing under the "
                        "heading is the truth")

    def test_atomic_write_replaces_and_leaves_no_temp(self):
        from tasks.core import _atomic_write
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text("old", encoding="utf-8")
        _atomic_write(tf, "new content")
        self.assertEqual(tf.read_text(encoding="utf-8"), "new content")
        self.assertEqual([p.name for p in d.iterdir()], ["task.md"],
                         "no temp residue may remain next to the task file")


if __name__ == "__main__":
    unittest.main()
