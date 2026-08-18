#!/usr/bin/env python3
"""1.5.3: judge.md round-stacking, tree-state freshness, per-transport budgets,
pinned sections, and the judge-execution (L1) prompt clause.

The failure classes these pin:
  * a re-run panel CLOBBERED judge.md — earlier rounds' verdicts (paid work,
    the record) were destroyed;
  * evidence was read by SUBSTRING over the whole file — a stale impl-PASS
    buried under a newer FAIL (or a newer plan round) would satisfy the close
    gate (the #09 disease, found by red-teaming our own design);
  * freshness was going to use mtimes — content fingerprints instead;
  * one argv-limited seat dictated every seat's context budget;
  * execution results without a prior hypothesis are vacuous greens.

Run: python3 tests/test_review_context_and_rounds.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.core import (  # noqa: E402
    has_panel_impl_evidence, parse_judge_rounds, resolve_review_context_chars,
    select_task_context, stack_judge_round, tree_state_fingerprint,
)
from tasks.template import judge_verify_clause, panel_impl_review_prompt  # noqa: E402


def round_text(mode, verdict, tree="abc123def456", n=1):
    return (f"# Panel {mode.title()} Review — task {n}\n\n"
            f"**PANEL VERDICT: {verdict}** — {n}/4, quorum 3\n"
            f"**Commit:** {'a' * 40}\n"
            f"**Tree-state:** {tree}\n\nfindings body {n}\n")


class ParseRounds(unittest.TestCase):
    def test_extracts_mode_verdict_tree(self):
        rounds = parse_judge_rounds(round_text("impl", "PASS", tree="deadbeef0001"))
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["mode"], "impl")
        self.assertEqual(rounds[0]["verdict"], "PASS")
        self.assertEqual(rounds[0]["tree_state"], "deadbeef0001")

    def test_multiple_rounds_file_order(self):
        text = round_text("impl", "FAIL", n=2) + "\n" + round_text("impl", "PASS", n=1)
        rounds = parse_judge_rounds(text)
        self.assertEqual([r["verdict"] for r in rounds], ["FAIL", "PASS"])

    def test_no_rounds_in_free_text(self):
        self.assertEqual(parse_judge_rounds("just some notes\nno headings\n"), [])


class StackRounds(unittest.TestCase):
    def _jm(self):
        return Path(tempfile.mkdtemp()) / "judge.md"

    def test_newest_first_nothing_clobbered(self):
        jm = self._jm()
        stack_judge_round(jm, round_text("impl", "FAIL", n=1))
        stack_judge_round(jm, round_text("impl", "PASS", n=2))
        rounds = parse_judge_rounds(jm.read_text(encoding="utf-8"))
        self.assertEqual([r["verdict"] for r in rounds], ["PASS", "FAIL"],
                         "newest round must be first; the old round must survive")

    def test_legacy_unparseable_content_is_preserved(self):
        jm = self._jm()
        jm.write_text("# Panel Panel — taskless mission\nold free-form verdicts\n",
                      encoding="utf-8")
        stack_judge_round(jm, round_text("impl", "PASS"))
        text = jm.read_text(encoding="utf-8")
        self.assertIn("old free-form verdicts", text,
                      "stacking must never destroy a record it cannot parse")

    def test_retention_trims_oldest_and_says_so(self):
        jm = self._jm()
        for i in range(7):
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        text = jm.read_text(encoding="utf-8")
        self.assertEqual(len(parse_judge_rounds(text)), 5)
        self.assertIn("older round(s) trimmed", text)


class StructuralEvidence(unittest.TestCase):
    """The red-team finding: substring checks over the whole file are how a
    stale or wrong-mode PASS satisfies the gate. Newest round decides."""

    def _task_with_judge(self, judge_text):
        d = Path(tempfile.mkdtemp())
        (d / "task.md").write_text("# 1 - t\n")
        (d / "judge.md").write_text(judge_text, encoding="utf-8")
        return d / "task.md"

    def test_newest_impl_pass_counts(self):
        tf = self._task_with_judge(round_text("impl", "PASS"))
        self.assertTrue(has_panel_impl_evidence(tf))

    def test_stale_pass_under_newer_fail_does_not_count(self):
        text = round_text("impl", "FAIL", n=2) + "\n" + round_text("impl", "PASS", n=1)
        tf = self._task_with_judge(text)
        self.assertFalse(has_panel_impl_evidence(tf),
                         "a newer FAIL must invalidate the buried PASS")

    def test_stale_impl_pass_under_newer_plan_round_does_not_count(self):
        text = round_text("plan", "PASS", n=2) + "\n" + round_text("impl", "PASS", n=1)
        tf = self._task_with_judge(text)
        self.assertFalse(has_panel_impl_evidence(tf),
                         "a newer plan round implies replanning — the old impl "
                         "PASS predates work that followed")


class TreeStateFingerprint(unittest.TestCase):
    def _repo(self):
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        (d / "code.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=d, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "seed"], cwd=d, check=True)
        return d

    def test_changes_when_code_changes(self):
        d = self._repo()
        fp1 = tree_state_fingerprint(d)
        self.assertTrue(fp1)
        (d / "code.py").write_text("x = 2\n")
        self.assertNotEqual(fp1, tree_state_fingerprint(d))

    def test_ignores_agent_dir_changes(self):
        # Triage edits task.md between panel and close BY DESIGN — the
        # fingerprint answers "did the CODE change?", or the advisory fires on
        # every close and gets ignored.
        d = self._repo()
        fp1 = tree_state_fingerprint(d)
        (d / ".agent" / "tasks" / "001-t").mkdir(parents=True)
        (d / ".agent" / "tasks" / "001-t" / "task.md").write_text("- [x] triage\n")
        self.assertEqual(fp1, tree_state_fingerprint(d))

    def test_empty_without_git(self):
        self.assertEqual(tree_state_fingerprint(Path(tempfile.mkdtemp())), "")


class TransportBudgets(unittest.TestCase):
    def _proj(self, cfg=None):
        d = Path(tempfile.mkdtemp())
        (d / ".agent").mkdir()
        if cfg:
            (d / ".agent" / "config.json").write_text(json.dumps(cfg))
        return d

    def setUp(self):
        for k in ("PLAYBOOK_REVIEW_CONTEXT_CHARS", "PLAYBOOK_REVIEW_CONTEXT_CHARS_STDIN"):
            os.environ.pop(k, None)

    def test_defaults_stdin_higher_than_argv(self):
        p = self._proj()
        argv = resolve_review_context_chars(p, stdin=False)
        stdin = resolve_review_context_chars(p, stdin=True)
        self.assertEqual(argv, 100_000)
        self.assertEqual(stdin, 200_000)
        self.assertGreater(stdin, argv)

    def test_config_overrides_each_key(self):
        p = self._proj({"review_context_chars": 50_000,
                        "review_context_chars_stdin": 400_000})
        self.assertEqual(resolve_review_context_chars(p, stdin=False), 50_000)
        self.assertEqual(resolve_review_context_chars(p, stdin=True), 400_000)

    def test_typo_small_value_falls_back(self):
        p = self._proj({"review_context_chars": 50})   # can't hold orientation
        self.assertEqual(resolve_review_context_chars(p, stdin=False), 100_000)

    def test_adapter_transports(self):
        from provider.adapters.claude import ClaudeAdapter
        from provider.adapters.codex import CodexAdapter
        from provider.adapters.grok import GrokAdapter
        from provider.adapters.pi import PiAdapter
        self.assertEqual(ClaudeAdapter.context_transport(), "stdin")
        self.assertEqual(CodexAdapter.context_transport(), "stdin")
        self.assertEqual(GrokAdapter.context_transport(), "argv")
        self.assertEqual(PiAdapter.context_transport(), "argv")


class PinnedSections(unittest.TestCase):
    def _sec(self, heading, chars, pinned=False):
        pin = "<!-- pin -->\n" if pinned else ""
        body = f"body-of-{heading.replace(' ', '-')} " * ((chars // 24) + 1)
        return f"## {heading}\n{pin}\n{body[:chars]}\n\n"

    # Budget 1000: Intent (~310) + Debrief (~130) fit; Round1 (~830) does NOT
    # fit on top — so ONLY the pin can save it. That is the feature: the pin
    # forces retention even past the budget, and the receipt warns.

    def test_pinned_old_section_survives_trim(self):
        text = (self._sec("Intent", 300)
                + self._sec("Round1 Triage", 800, pinned=True)
                + self._sec("Fat Middle", 9000)
                + self._sec("Debrief", 100))
        out, receipt = select_task_context(text, 1000)
        self.assertIn("## Round1 Triage", out, "pinned section must survive any trim")
        self.assertNotIn("body-of-Fat-Middle", out)
        self.assertIn("WARNING", receipt, "pin past the budget must be receipted loudly")

    def test_unpinned_equivalent_is_dropped(self):
        text = (self._sec("Intent", 300)
                + self._sec("Round1 Triage", 800, pinned=False)
                + self._sec("Fat Middle", 9000)
                + self._sec("Debrief", 100))
        out, receipt = select_task_context(text, 1000)
        self.assertNotIn("## Round1 Triage", out)
        self.assertIn("Round1 Triage", receipt, "the drop must be named")


class JudgeVerifyClause(unittest.TestCase):
    def test_empty_when_undeclared(self):
        self.assertEqual(judge_verify_clause(None), "")
        self.assertEqual(judge_verify_clause([]), "")
        p = panel_impl_review_prompt(".agent/tasks/001-x/task.md")
        self.assertNotIn("EXECUTION", p)

    def test_rules_present_when_declared(self):
        clause = judge_verify_clause(["python3 -m unittest discover"])
        for must in ("predicted outcome BEFORE", "reproduce", "timing",
                     "python3 -m unittest discover", "SPECIFIC suspicion"):
            self.assertIn(must, clause)

    def test_prompt_carries_clause_and_trim_notice(self):
        p = panel_impl_review_prompt(
            ".agent/tasks/001-x/task.md",
            trim_notice="your inline copy was TRIMMED (dropped: Work Plan) — read the file.",
            judge_verify=["make check"])
        self.assertIn("CONTEXT NOTE:", p)
        self.assertIn("dropped: Work Plan", p)
        self.assertIn("`make check`", p)

    def test_non_string_entries_ignored(self):
        self.assertEqual(judge_verify_clause([7, "", None]), "")


if __name__ == "__main__":
    unittest.main()
