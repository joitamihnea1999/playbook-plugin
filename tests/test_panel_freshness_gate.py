#!/usr/bin/env python3
"""F18 — irreversible closes resolve panel staleness explicitly (blind-judge
reviewed design: design-1.5.6.md + judge-f18-design.md, all findings built).

Three layers:

  * `tree_state_fingerprint` coverage (judge C1: the old fingerprint was
    PROVABLY blind to edits in untracked files — where batches 4 and 5 put
    their post-panel fixes) + config-declared excludes (judge C2: standing-
    gate outputs like journal/NNN.md must be excludable or the gate blocks
    100% of well-behaved closes on the flagship project);
  * the pure gate decision (`freshness_gate_decision`);
  * the real CLI close path: block matrix, narrow override, receipt clauses
    (judge F4: a missing stamp is RECORDED, not silently skipped; judge F5:
    a shared --reason under --force attributes to force).

Run: python3 -m unittest tests.test_panel_freshness_gate
"""
from __future__ import annotations

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

from tasks.core import freshness_gate_decision, tree_state_fingerprint  # noqa: E402

BLOCK_MARKER = "code state changed after the newest impl panel"
CLAUSE = "**Panel tree-state:**"


def _git(d, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=d, check=True, capture_output=True)


def _repo() -> Path:
    d = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "code.py").write_text("x = 1\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "seed")
    return d


class FingerprintCoverage(unittest.TestCase):
    def test_untracked_file_edit_moves_the_fingerprint(self):
        # Judge C1, reproduced red against the 1.5.3 fingerprint: porcelain
        # names an untracked file but not its content; diff HEAD skips it.
        d = _repo()
        (d / "new_module.py").write_text("VERSION = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        (d / "new_module.py").write_text("VERSION = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2,
                            "fingerprint is blind to untracked-file edits — "
                            "the batch-4/5 post-panel fix shape")

    def test_untracked_file_inside_new_directory_covered(self):
        # porcelain without -uall shows only `?? dir/` — the file inside is
        # invisible unless untracked enumeration is per-file.
        d = _repo()
        (d / "pkg").mkdir()
        (d / "pkg" / "mod.py").write_text("a = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        (d / "pkg" / "mod.py").write_text("a = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2)

    def test_agent_dir_still_excluded(self):
        # Negative control: the original exclusion must survive the rewrite —
        # triage edits between panel and close are the designed flow.
        d = _repo()
        (d / ".agent" / "tasks" / "001-t").mkdir(parents=True)
        fp1 = tree_state_fingerprint(d)
        (d / ".agent" / "tasks" / "001-t" / "task.md").write_text("- [x] g\n",
                                                                  encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertEqual(fp1, fp2)

    def test_config_declared_exclude_is_honored(self):
        # Judge C2: owner-declared bookkeeping (StrataDB journal/) must be
        # excludable, or the irreversible gate fires on every close.
        d = _repo()
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(
            json.dumps({"fingerprint_exclude": ["journal/"]}), encoding="utf-8")
        (d / "journal").mkdir()
        fp1 = tree_state_fingerprint(d)
        (d / "journal" / "011.md").write_text("shipped\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertEqual(fp1, fp2, "declared exclude did not suppress journal noise")
        # and a real code edit still moves it (exclusion is narrow)
        (d / "code.py").write_text("x = 3\n", encoding="utf-8")
        self.assertNotEqual(fp2, tree_state_fingerprint(d))

    def test_malformed_exclude_skipped_loudly_not_silently(self):
        d = _repo()
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(
            json.dumps({"fingerprint_exclude": ["ok/", 7, ""]}), encoding="utf-8")
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fp = tree_state_fingerprint(d)
        self.assertTrue(fp, "malformed entries must not kill the fingerprint")
        self.assertIn("fingerprint_exclude", buf.getvalue(),
                      "malformed entries must be skipped LOUDLY")


class GateDecision(unittest.TestCase):
    KW = dict(risk="irreversible", panel_required=True, evidence_carries=True,
              round_fp="a" * 12, now_fp="b" * 12, force=False,
              stale_ok=False, stale_reason=None)

    def test_stale_irreversible_blocks(self):
        allowed, why = freshness_gate_decision(**self.KW)
        self.assertFalse(allowed)
        self.assertIn("re-run", why)
        self.assertIn("--stale-panel-ok", why)

    def test_override_without_reason_refused(self):
        allowed, why = freshness_gate_decision(**{**self.KW, "stale_ok": True})
        self.assertFalse(allowed)
        self.assertIn("--reason", why)

    def test_override_with_reason_allows(self):
        allowed, _ = freshness_gate_decision(
            **{**self.KW, "stale_ok": True, "stale_reason": "journal only"})
        self.assertTrue(allowed)

    def test_gate_scope_negative_controls(self):
        # Each condition individually off → allowed (the gate is narrow).
        for tweak in ({"risk": "reversible"}, {"risk": "assertive"},
                      {"panel_required": False}, {"evidence_carries": False},
                      {"round_fp": ""}, {"now_fp": ""},
                      {"now_fp": "a" * 12}, {"force": True}):
            allowed, _ = freshness_gate_decision(**{**self.KW, **tweak})
            self.assertTrue(allowed, f"gate overreached with {tweak}")


class ClosePathMatrix(unittest.TestCase):
    """End-to-end through the real CLI."""

    def _setup(self, *, risk: str, panel_cfg, round_head: str = "Impl",
               verdict: str = "PASS", stamp: bool = True,
               change_after: bool = True, extra_round: "str | None" = None):
        d = _repo()
        (d / ".agent").mkdir(exist_ok=True)
        if panel_cfg is not None:
            (d / ".agent" / "config.json").write_text(
                json.dumps({"panel_required_for": panel_cfg}), encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            f"# 001 - T\n\n## Status\npending\n\n## Risk\n{risk}\n\n"
            "## Work Plan\n- [x] G1: do it\n", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-f18")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        fp = tree_state_fingerprint(d)
        stamp_line = f"**Tree-state:** {fp}\n" if stamp else ""
        rounds = (f"# Panel {round_head} Review — task 1\n\n"
                  f"**PANEL VERDICT: {verdict}** — 4/4, quorum 3\n"
                  f"{stamp_line}\nbody\n")
        if extra_round:
            rounds = extra_round + "\n" + rounds
        (td / "judge.md").write_text(rounds, encoding="utf-8")
        if change_after:
            (d / "code.py").write_text("x = 99\n", encoding="utf-8")
        return d, td, env

    def _close(self, d, env, *flags):
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", "work", "done", *flags],
            cwd=d, env=env, capture_output=True, text=True, timeout=60)

    def _receipt(self, td) -> str:
        return (td / "task.md").read_text(encoding="utf-8")

    def test_irreversible_stale_blocks(self):
        d, td, env = self._setup(risk="irreversible", panel_cfg="all")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        self.assertIn("pending", self._receipt(td))

    def test_override_needs_reason_then_closes_with_recorded_reason(self):
        d, td, env = self._setup(risk="irreversible", panel_cfg="all")
        r = self._close(d, env, "--stale-panel-ok")
        self.assertNotIn("Task 001 done.", r.stdout)
        r = self._close(d, env, "--stale-panel-ok", "--reason", "journal only, diff reviewed")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn(CLAUSE, receipt)
        self.assertIn("STALE", receipt)
        self.assertIn('accepted: "journal only, diff reviewed"', receipt)

    def test_irreversible_fresh_closes_with_fresh_clause(self):
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 change_after=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("FRESH", self._receipt(td))

    def test_reversible_stale_closes_with_stale_clause(self):
        # Advisory behavior unchanged below irreversible; the record is new.
        d, td, env = self._setup(risk="reversible", panel_cfg="all")
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))
        self.assertIn("tree-state mismatch", r.stdout)  # console note kept

    def test_irreversible_stale_without_policy_is_advisory(self):
        # Judge A4: a voluntary panel in a no-policy project must not block.
        d, td, env = self._setup(risk="irreversible", panel_cfg=None)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))

    def test_no_impl_round_gets_panel_evidence_block_not_freshness(self):
        # Judge C3 / design A3: exactly one block message.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 round_head="Plan")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("panel review required by policy", r.stderr)
        self.assertNotIn(BLOCK_MARKER, r.stderr)

    def test_newest_impl_fail_round_skips_freshness_gate(self):
        # Judge C3 case 1: a stamped FAIL round cannot carry the close, so the
        # freshness gate must stay silent and the evidence block must fire.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 verdict="FAIL")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("panel review required by policy", r.stderr)
        self.assertNotIn(BLOCK_MARKER, r.stderr)

    def test_replan_over_stamped_impl_skips_freshness_gate(self):
        # Judge C3 case 2: newest round is plan → evidence does not carry.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 round_head="Plan", verdict="PASS",
                                 extra_round=None)
        # build: plan round newest (written by _setup), stamped impl BELOW it
        fp = tree_state_fingerprint(d)
        jm = td / "judge.md"
        jm.write_text(jm.read_text(encoding="utf-8")
                      + f"\n# Panel Impl Review — task 1\n\n"
                        f"**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
                        f"**Tree-state:** {fp}\n\nolder impl\n", encoding="utf-8")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("panel review required by policy", r.stderr)
        self.assertNotIn(BLOCK_MARKER, r.stderr)

    def test_missing_stamp_recorded_not_silent(self):
        # Judge F4: deleting/never-having the stamp must leave a record, not
        # read as legacy silence.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 stamp=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("no stamp recorded", self._receipt(td))

    def test_force_bypasses_and_attributes_reason_to_force(self):
        # Judge F5 / design A8: --force keeps whole-policy semantics; the
        # shared --reason lands as the FORCED reason, and the freshness clause
        # still records STALE (Leg 1 is unconditional).
        d, td, env = self._setup(risk="irreversible", panel_cfg="all")
        r = self._close(d, env, "--force", "--reason", "emergency close")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn("Forced close, reason:", receipt)
        self.assertIn("emergency close", receipt)
        self.assertIn("STALE", receipt)
        self.assertNotIn('accepted: "emergency close"', receipt)


if __name__ == "__main__":
    unittest.main()
