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


if __name__ == "__main__":
    unittest.main()
