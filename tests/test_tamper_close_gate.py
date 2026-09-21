#!/usr/bin/env python3
"""A DEGRADED tamper guard cannot certify a high-consequence close (task 059,
plan panel P2 — opus).

Task 059 made a degraded guard (git could not fully run) advisory on the panel
and single-judge paths: the paid verdict is KEPT and the round header records
`**Tamper guard:** degraded — …`. That header is also what the close accepts
as impl-panel evidence — so on Windows (uncontained: the guard is the only
defense) an assertive/irreversible close would otherwise proceed on a PASS
whose tamper-freedom was never verified. These tests pin the other half: the
close treats such a round like a STALE one — block with the same two exits
(re-run the panel / `--stale-panel-ok --reason`, recorded), advisory for
`reversible`, and a round WITHOUT the line (pre-059) stays clean.

Pure stdlib unittest. Run: python3 -m unittest tests.test_tamper_close_gate
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

from tasks.core import (  # noqa: E402
    format_verify_receipt, freshness_gate_decision, parse_judge_rounds,
    tree_state_fingerprint,
)


def _git(d, *args):
    subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True)


def _repo() -> Path:
    d = Path(tempfile.mkdtemp())
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "x@y.z")
    _git(d, "config", "user.name", "x")
    (d / "code.py").write_text("x = 1\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "seed")
    return d


DEGRADED = ("**Tamper guard:** degraded — content-hash guard degraded: could not "
            "enumerate dirty files at close (`git status -z` failed)")


class ParserReadsTheLine(unittest.TestCase):
    def _round(self, guard_line):
        return (f"# Panel Impl Review — t\n\n**PANEL VERDICT: PASS** — 5/5\n"
                f"**Commit:** abc\n{guard_line}**Tree-state:** deadbeef\n\nbody\n")

    def test_degraded_is_parsed_with_detail(self):
        r = parse_judge_rounds(self._round(DEGRADED + "\n"))[0]
        self.assertEqual(r["tamper_guard"], "degraded")
        self.assertIn("git status -z", r["tamper_detail"])

    def test_clean_is_parsed(self):
        r = parse_judge_rounds(self._round("**Tamper guard:** clean\n"))[0]
        self.assertEqual(r["tamper_guard"], "clean")

    def test_absent_line_is_none_not_degraded(self):
        r = parse_judge_rounds(self._round(""))[0]
        self.assertIsNone(r["tamper_guard"])

    def test_line_after_the_judge_separator_is_ignored(self):
        # A prompt-injected JUDGE cannot forge (or clear) the guard receipt: only
        # the orchestrator-authored header region counts.
        body = self._round("") + "\n" + "═" * 60 + "\n**Tamper guard:** clean\n"
        r = parse_judge_rounds(body)[0]
        self.assertIsNone(r["tamper_guard"])


class GateDecision(unittest.TestCase):
    KW = dict(risk="assertive", panel_required=True, evidence_carries=True,
              round_fp="abc", now_fp="abc", force=False, stale_ok=False,
              stale_reason=None, git_available=True, exclude_covers=None)

    def test_degraded_blocks_a_fresh_assertive_close(self):
        allowed, why = freshness_gate_decision(**{**self.KW, "tamper_degraded": True})
        self.assertFalse(allowed)
        self.assertIn("TAMPER-GUARD-DEGRADED", why)
        self.assertIn("--stale-panel-ok", why)

    def test_degraded_blocks_irreversible_and_unclassified(self):
        for risk in ("irreversible", "unclassified"):
            allowed, _ = freshness_gate_decision(**{**self.KW, "risk": risk, "tamper_degraded": True})
            self.assertFalse(allowed, risk)

    def test_reversible_is_advisory(self):
        allowed, _ = freshness_gate_decision(**{**self.KW, "risk": "reversible", "tamper_degraded": True})
        self.assertTrue(allowed)

    def test_override_needs_a_reason(self):
        allowed, why = freshness_gate_decision(**{**self.KW, "tamper_degraded": True, "stale_ok": True})
        self.assertFalse(allowed)
        self.assertIn("--reason", why)
        allowed, _ = freshness_gate_decision(
            **{**self.KW, "tamper_degraded": True, "stale_ok": True, "stale_reason": "guard degraded by a known git quirk; diff inspected"})
        self.assertTrue(allowed)

    def test_clean_guard_changes_nothing(self):
        allowed, _ = freshness_gate_decision(**{**self.KW, "tamper_degraded": False})
        self.assertTrue(allowed)


class ReceiptNamesEveryGatingCondition(unittest.TestCase):
    """059 impl-panel r3 (sonnet #2): when EXCLUDE-COVERS-CODE / NO-STAMP /
    UNREADABLE was already the verdict, the tamper degradation the operator was
    actually blocked on (and accepted) vanished from the receipt."""

    def test_degraded_guard_is_recorded_beside_another_verdict(self):
        txt = format_verify_receipt([], "abc", "assertive", freshness={
            "verdict": "EXCLUDE-COVERS-CODE", "paths": ["src/app.py"],
            "round_fp": "abc", "now_fp": "abc",
            "tamper_degraded_detail": "content-hash guard degraded: `git status -z` failed",
            "accepted_reason": "inspected by hand"})
        self.assertIn("EXCLUDE-COVERS-CODE", txt)
        self.assertIn("tamper guard was DEGRADED", txt)
        self.assertIn("git status -z", txt)

    def test_nothing_extra_when_the_guard_was_clean(self):
        txt = format_verify_receipt([], "abc", "assertive", freshness={
            "verdict": "STALE", "round_fp": "a", "now_fp": "b"})
        self.assertNotIn("tamper guard was DEGRADED", txt)


class Receipt(unittest.TestCase):
    def test_receipt_names_the_degraded_guard(self):
        txt = format_verify_receipt([], "abc", "assertive", freshness={
            "verdict": "TAMPER-GUARD-DEGRADED", "round_fp": "abc", "now_fp": "abc",
            "detail": "git status -z failed", "accepted_reason": "inspected by hand"})
        self.assertIn("TAMPER-GUARD-DEGRADED", txt)
        self.assertIn("git status -z failed", txt)
        self.assertIn('accepted: "inspected by hand"', txt)


class ClosePath(unittest.TestCase):
    """End to end through the real CLI: a PASS impl round whose header says the
    guard was degraded, on a tree that did NOT change (so freshness alone would
    read FRESH)."""

    def _setup(self, *, risk, guard_line=DEGRADED + "\n"):
        d = _repo()
        (d / ".agent").mkdir(exist_ok=True)
        (d / ".agent" / "config.json").write_text(
            json.dumps({"panel_required_for": ["assertive", "irreversible"]}), encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            f"# 001 - T\n\n## Status\npending\n\n## Risk\n{risk}\n\n## Work Plan\n- [x] G1: do it\n",
            encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-059")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        fp = tree_state_fingerprint(d)
        (td / "judge.md").write_text(
            f"# Panel Impl Review — task 1\n\n**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
            f"**Commit:** abc\n{guard_line}**Tree-state:** {fp}\n\nbody\n", encoding="utf-8")
        return d, td, env

    def _close(self, d, env, *flags):
        return subprocess.run([sys.executable, "-m", "tasks.cli", "work", "done", *flags],
                              cwd=d, env=env, capture_output=True, text=True, timeout=120)

    def test_assertive_degraded_blocks_then_closes_with_recorded_reason(self):
        d, td, env = self._setup(risk="assertive")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout, r.stdout + r.stderr)
        self.assertIn("TAMPER-GUARD-DEGRADED", r.stdout + r.stderr)
        r = self._close(d, env, "--stale-panel-ok", "--reason", "git -z quirk on this host; tree inspected")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = (td / "task.md").read_text(encoding="utf-8")
        self.assertIn("TAMPER-GUARD-DEGRADED", receipt)
        self.assertIn('accepted: "git -z quirk on this host; tree inspected"', receipt)

    def test_reversible_degraded_closes_with_an_advisory(self):
        d, td, env = self._setup(risk="reversible")
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("tamper guard", (r.stdout + r.stderr).lower())

    def test_pre_059_round_without_the_line_still_closes(self):
        d, td, env = self._setup(risk="assertive", guard_line="")
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertNotIn("TAMPER-GUARD-DEGRADED", r.stdout + r.stderr)


class CloseGitProbeHonoursCeilings(unittest.TestCase):
    """059 impl-panel r2 (sonnet #2): the close re-implemented the `.git`
    ancestor walk inline, without the ceiling/mount rules W5 added to
    `review._in_git_repo` — the same T009#2 false positive at a second call
    site. Both paths must now use the ONE shared helper."""

    def test_core_exports_the_shared_probe(self):
        from tasks.core import in_git_repo
        from tasks.review import _in_git_repo
        self.assertIs(_in_git_repo, in_git_repo)

    def test_shared_probe_honours_the_ceiling(self):
        from tasks.core import in_git_repo
        from unittest import mock
        d = _repo()
        app = d / "app"
        app.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(d)}):
            self.assertFalse(in_git_repo(app))
        self.assertTrue(in_git_repo(app))

    def test_lifecycle_uses_it(self):
        import inspect
        from tasks import lifecycle
        src = inspect.getsource(lifecycle)
        self.assertIn("in_git_repo(", src)
        self.assertNotIn('if (_gp / ".git").exists():', src,
                         "the close still carries its own naive ancestor walk")


class SingleJudgeDegradedAtClose(unittest.TestCase):
    """059 impl-panel r1 (sonnet #1/#3): the close-time TAMPER-GUARD-DEGRADED
    protection read only PANEL rounds, so a single-judge review that ran with a
    degraded guard — the common case for `reversible` work, where
    `panel_required_for` demands no panel — produced ZERO close-time signal.
    It now surfaces as an advisory note and a receipt clause."""

    MARK = ("[context] full task.md + mind map delivered (no truncation)\n"
            "[tamper guard] degraded — content-hash guard degraded: `git status -z` failed\n\n"
            "1. **Note** — fine.\n")

    def _reader(self):
        from tasks.core import single_review_tamper_degraded
        return single_review_tamper_degraded

    def _task(self, log_body=None, name="judge.log"):   # the REAL claude log name
        d = _repo()
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text("# 001 - T\n\n## Status\npending\n\n## Risk\nreversible\n\n"
                      "## Work Plan\n- [x] G1: do it\n", encoding="utf-8")
        if log_body is not None:
            (td / name).write_text(log_body, encoding="utf-8")
        return d, td, tf

    def test_reader_finds_the_marker(self):
        d, td, tf = self._task(self.MARK)
        detail = self._reader()(tf)
        self.assertIsNotNone(detail)
        self.assertIn("git status -z", detail)

    def test_reader_is_silent_on_a_clean_log(self):
        d, td, tf = self._task(self.MARK.replace("degraded — content-hash guard degraded: `git status -z` failed", "clean"))
        self.assertIsNone(self._reader()(tf))

    def test_reader_is_silent_without_a_log(self):
        d, td, tf = self._task(None)
        self.assertIsNone(self._reader()(tf))

    def test_reader_reads_the_newest_log(self):
        import time
        d, td, tf = self._task(self.MARK, name="judge.log")
        time.sleep(0.02)
        (td / "judge-codex.log").write_text(
            "[context] x\n[tamper guard] clean\n\nfindings\n", encoding="utf-8")
        self.assertIsNone(self._reader()(tf), "the newest review's receipt is the one that counts")

    def test_reader_covers_every_real_backend_log_name(self):
        # 059 impl-panel r2 (grok #2): the reader globbed `judge-*.log`, which
        # MISSES the supported Claude backend's own `judge.log` — and the first
        # version of these tests planted a name production never writes.
        from tasks.review import _judge_log_name
        for backend in ("claude", "codex", "antigravity", "grok", "pi"):
            with self.subTest(backend=backend):
                d, td, tf = self._task(self.MARK, name=_judge_log_name(backend))
                self.assertIsNotNone(self._reader()(tf), _judge_log_name(backend))

    def test_name_list_matches_reviews_own_mapping(self):
        from tasks.core import judge_log_names
        from tasks.review import _judge_log_name
        for backend in ("claude", "codex", "antigravity", "grok", "pi"):
            self.assertIn(_judge_log_name(backend), judge_log_names(), backend)

    def test_partial_log_receipt_is_read_too(self):
        d, td, tf = self._task(self.MARK, name="judge.partial.log")
        self.assertIsNotNone(self._reader()(tf))

    def test_close_records_the_single_judge_degradation(self):
        d, td, tf = self._task(self.MARK)
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-059s")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "done"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=120)
        self.assertIn("Task 001 done.", r.stdout, r.stdout + r.stderr)
        self.assertIn("tamper guard", (r.stdout + r.stderr).lower())
        receipt = tf.read_text(encoding="utf-8")
        self.assertIn("single-judge review ran with a DEGRADED tamper guard", receipt)


if __name__ == "__main__":
    unittest.main()
