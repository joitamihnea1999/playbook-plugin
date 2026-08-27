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

    def test_retention_caps_judgemd_and_points_to_archive(self):
        # S2 (1.5.41): judge.md still holds only the newest max_rounds (review
        # budget), but the overflow is ARCHIVED, not destroyed — and the in-file
        # pointer names the archive so a reader knows where the rest went.
        jm = self._jm()
        for i in range(7):
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        text = jm.read_text(encoding="utf-8")
        self.assertEqual(len(parse_judge_rounds(text)), 5)
        self.assertIn("judge-archive.md", text,
                      "the pointer must name the archive, not claim git")
        self.assertNotIn("full history is in git", text,
                         "the unreliable git-history claim must be gone")

    def test_overflow_round_archived_verbatim_not_destroyed(self):
        # The core defect: the 6th stack used to DROP the oldest round with only
        # a count. Now the oldest round's body must survive verbatim in the
        # sibling archive.
        jm = self._jm()
        for i in range(6):  # rounds 0..5 → judge.md keeps 5..1, round 0 overflows
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        jm_text = jm.read_text(encoding="utf-8")
        self.assertNotIn("findings body 0", jm_text,
                         "oldest round should be out of judge.md's window")
        archive = jm.parent / "judge-archive.md"
        self.assertTrue(archive.exists(), "overflow round was not archived")
        arc_text = archive.read_text(encoding="utf-8")
        self.assertIn("findings body 0", arc_text,
                      "the trimmed round must be preserved verbatim in the archive")
        # And it must be a real, parseable round (not just loose text).
        self.assertEqual(len(parse_judge_rounds(arc_text)), 1)
        # Round-4 panel G3: prove "verbatim" for real — the archived body must
        # equal the original round, and no trim-pointer may leak into it.
        self.assertEqual(parse_judge_rounds(arc_text)[0]["body"].rstrip(),
                         round_text("impl", "PASS", n=0).rstrip(),
                         "archived round body must be byte-identical to the original")
        self.assertNotIn("older round(s)", arc_text,
                         "no trim-pointer text may leak into the archive")

    def test_rearchiving_only_present_rounds_is_a_true_noop(self):
        # Round-4 panel G3/grok: when every overflow body is already archived
        # (a crash-retry re-presenting the same bodies), the helper must not
        # rewrite the archive at all — a needless rewrite could fail and push
        # the caller onto the keep-in-both-files path, inflating the count.
        import tasks.core as core
        jm = self._jm()
        archive = jm.parent / "judge-archive.md"
        body = round_text("impl", "PASS", n=0).rstrip()
        core._archive_judge_overflow(archive, [body])  # archive now holds round 0
        calls = []
        real_aw = core._atomic_write

        def spy_aw(path, text):
            calls.append(Path(path).name)
            return real_aw(path, text)

        core._atomic_write = spy_aw
        try:
            n = core._archive_judge_overflow(archive, [body])  # all already present
        finally:
            core._atomic_write = real_aw
        self.assertEqual(n, 1, "count must still reflect the one archived round")
        self.assertEqual(calls, [],
                         "a no-op re-archive must not rewrite judge-archive.md")

    def test_archived_bodies_are_pointer_free_and_verbatim_past_first_overflow(self):
        # Round-5 panel G2 (grok): the 6-stack verbatim test only archives round 0,
        # which never carried a trim-pointer. From the 2nd overflow on, the
        # oldest kept body in judge.md carries the end-of-file pointer, so
        # `_TRIM_POINTER_RE` MUST strip it before archiving. Drive 8 stacks and
        # assert every archived body is byte-identical to its original round and
        # that no pointer text leaked into the archive.
        jm = self._jm()
        for i in range(8):  # rounds 0..7 → 2,1,0 overflow into the archive
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        arc_text = (jm.parent / "judge-archive.md").read_text(encoding="utf-8")
        self.assertNotIn("older round(s)", arc_text,
                         "a trim-pointer leaked into the archive (strip failed)")
        archived = parse_judge_rounds(arc_text)
        self.assertEqual(len(archived), 3)
        got = {r["body"].rstrip() for r in archived}
        want = {round_text("impl", "PASS", n=n).rstrip() for n in (0, 1, 2)}
        self.assertEqual(got, want,
                         "archived bodies must equal their originals verbatim, "
                         "with the trim-pointer stripped")

    def test_archive_accumulates_newest_first_judgemd_stays_capped(self):
        jm = self._jm()
        for i in range(10):  # rounds 0..9
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        jm_text = jm.read_text(encoding="utf-8")
        self.assertEqual(len(parse_judge_rounds(jm_text)), 5)
        arc_text = (jm.parent / "judge-archive.md").read_text(encoding="utf-8")
        # rounds 0..4 overflowed (5 total), kept newest-first in the archive.
        self.assertEqual(len(parse_judge_rounds(arc_text)), 5)
        self.assertLess(arc_text.index("findings body 4"),
                        arc_text.index("findings body 0"),
                        "archive must be newest-first (round 4 before round 0)")

    def test_archive_is_idempotent_on_duplicate_overflow(self):
        # Round-2 panel F1 (opus): a crash between the archive write and the
        # judge.md write, followed by a retry, recomputes the same overflow and
        # re-archives it — which would double-count and inflate the documented
        # "true round count". Archiving the same round body twice must be a
        # no-op the second time.
        from tasks.core import _archive_judge_overflow
        jm = self._jm()
        archive = jm.parent / "judge-archive.md"
        body = round_text("impl", "PASS", n=0).rstrip()
        _archive_judge_overflow(archive, [body])
        _archive_judge_overflow(archive, [body])  # retry after a crash
        self.assertEqual(
            len(parse_judge_rounds(archive.read_text(encoding="utf-8"))), 1,
            "re-archiving the identical round must not duplicate it")

    def test_no_archive_created_within_cap(self):
        jm = self._jm()
        for i in range(5):  # exactly at the cap → nothing overflows
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        self.assertEqual(len(parse_judge_rounds(jm.read_text(encoding="utf-8"))), 5)
        self.assertFalse((jm.parent / "judge-archive.md").exists(),
                         "no archive should exist when nothing was trimmed")

    def test_archive_failure_retains_overflow_in_judgemd_never_loses_it(self):
        # F1 (round-2 panel): if the archive cannot be written (here: a DIRECTORY
        # sits at its path, so it is unreadable/unwritable), the overflow round
        # must NOT be deleted — the whole point of this task is that a paid round
        # is never lost. judge.md keeps ALL rounds untrimmed and says why.
        jm = self._jm()
        (jm.parent / "judge-archive.md").mkdir()  # sabotage the archive path
        for i in range(6):
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        text = jm.read_text(encoding="utf-8")
        self.assertIn("findings body 0", text,
                      "overflow round must be retained in judge.md when archiving fails")
        self.assertEqual(len(parse_judge_rounds(text)), 6,
                         "no paid round may be dropped on the archive-failure path")
        self.assertNotIn("recover from git history", text,
                         "must not claim git recovery while retaining in-file")

    def test_unparseable_existing_archive_is_preserved_not_overwritten(self):
        # F2: an archive that already holds content parse_judge_rounds can't
        # structure (legacy/opaque) must survive the next overflow, not be
        # clobbered — same 'never destroy a record you can't parse' rule as
        # judge.md's own stacker.
        jm = self._jm()
        archive = jm.parent / "judge-archive.md"
        archive.write_text("LEGACY OPAQUE ARCHIVE CONTENT\n", encoding="utf-8")
        for i in range(6):  # forces one overflow into the archive
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        arc_text = archive.read_text(encoding="utf-8")
        self.assertIn("LEGACY OPAQUE ARCHIVE CONTENT", arc_text,
                      "opaque prior archive content must be preserved verbatim")
        self.assertIn("findings body 0", arc_text,
                      "the new overflow round must be archived alongside it")

    def test_judgemd_write_failure_rolls_back_archive(self):
        # F3: archive is written before judge.md; if the judge.md write then
        # fails, the archive must be rolled back so a retry cannot double-archive
        # the same overflow (which would inflate the documented count). Fail ONLY
        # the judge.md write (a directory-at-path would also break the read and
        # produce no overflow, so patch the writer instead).
        import tasks.core as core
        jm = self._jm()
        for i in range(5):  # 5 rounds, at cap, no archive yet
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        archive = jm.parent / "judge-archive.md"
        self.assertFalse(archive.exists())
        real_aw = core._atomic_write

        def failing_aw(path, text):
            if Path(path).name == "judge.md":
                raise OSError("simulated judge.md write failure")
            return real_aw(path, text)

        core._atomic_write = failing_aw
        try:
            with self.assertRaises(OSError):
                stack_judge_round(jm, round_text("impl", "PASS", n=5), max_rounds=5)
        finally:
            core._atomic_write = real_aw
        self.assertFalse(archive.exists(),
                         "archive must be rolled back when the judge.md write fails")
        # the pre-existing judge.md record is untouched (still its 5 rounds).
        self.assertEqual(len(parse_judge_rounds(jm.read_text(encoding="utf-8"))), 5)

    def test_judgemd_write_failure_restores_existing_archive_byte_exact(self):
        # Round-3 panel (sonnet + grok, convergent): the F3 rollback's
        # archive_existed=True branch (restore prior BYTES) was untested and used
        # a non-atomic truncate-then-write. Seed an already-populated archive,
        # fail only the judge.md write on a later overflow, and assert the
        # archive is restored to its EXACT prior bytes (not truncated/lost).
        import tasks.core as core
        jm = self._jm()
        for i in range(6):  # 6 stacks → one overflow already sits in the archive
            stack_judge_round(jm, round_text("impl", "PASS", n=i), max_rounds=5)
        archive = jm.parent / "judge-archive.md"
        self.assertTrue(archive.exists())
        prior_bytes = archive.read_bytes()
        real_aw = core._atomic_write

        def failing_aw(path, text):
            if Path(path).name == "judge.md":
                raise OSError("simulated judge.md write failure")
            return real_aw(path, text)

        core._atomic_write = failing_aw
        try:
            with self.assertRaises(OSError):
                stack_judge_round(jm, round_text("impl", "PASS", n=99), max_rounds=5)
        finally:
            core._atomic_write = real_aw
        self.assertEqual(archive.read_bytes(), prior_bytes,
                         "existing archive must be restored byte-for-byte on rollback")


class ArchiveDocFormat(unittest.TestCase):
    """S2 (1.5.41): the true round format must be documented so lens v0.2 can
    count judge.md + judge-archive.md instead of undercounting at 5."""

    def test_architecture_doc_states_format_and_archive(self):
        doc = (_HERE.parent / "docs" / "architecture.md").read_text(
            encoding="utf-8")
        self.assertIn("judge-archive.md", doc)
        self.assertIn("# Panel Impl Review", doc,
                      "the round-heading format must be documented for counters")
        self.assertIn("true panel-round count", doc.lower())
        # Round-5 panel G3 (grok): pin the ADDITIVE rule, not just the phrase —
        # a doc that still said "count judge.md alone" would pass the substring
        # checks above but re-introduce the original undercount.
        low = doc.lower()
        self.assertIn("judge-archive.md", low)
        self.assertTrue(
            ("plus those in `judge-archive.md`" in doc)
            or ("headings in **both**" in doc)
            or ("headings in both" in low),
            "the doc must state the count is judge.md headings PLUS "
            "judge-archive.md headings, not judge.md alone")


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
