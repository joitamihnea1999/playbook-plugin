#!/usr/bin/env python3
"""The close-time tree-state freshness advisory fires on a real mismatch (F17).

1.5.3 shipped the advisory: panels stamp `**Tree-state:**` (content fingerprint,
`.agent/` excluded) into judge.md, and `tasks work done` prints a note when the
newest impl round's fingerprint no longer matches the code. On StrataDB task
010 the code changed after the impl panel — the exact trigger — but the
advisory prints to console and is not persisted, so no artifact could prove
whether it fired, and no test exercised the close path at all (the 1.5.3 tests
cover the fingerprint function and the round parser, not the wiring).

These tests close that gap end-to-end through the real CLI:

  * code changed after the stamped impl round → the advisory prints (the 010
    scenario, reproduced);
  * fingerprint still matching → silent (negative control — an advisory that
    fires on every close gets ignored, the F16 lesson);
  * no impl round at all → silent, close unharmed.

Run: python3 -m unittest tests.test_freshness_advisory
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

from tasks.core import tree_state_fingerprint  # noqa: E402

ADVISORY_MARKER = "tree-state mismatch"


def impl_round(fp: str) -> str:
    return (f"# Panel Impl Review — task 1\n\n"
            f"**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
            f"**Commit:** {'a' * 40}\n"
            f"**Tree-state:** {fp}\n\nfindings body\n")


class FreshnessAdvisoryAtClose(unittest.TestCase):
    def _close(self, *, judge_md: "str | None", change_after_panel: bool):
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        (d / "code.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=d, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "seed"], cwd=d, check=True)
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            "# 001 - T\n\n## Status\npending\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1: do it\n", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-f17")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        if judge_md is not None:
            # The stamp panels write at review time — taken NOW, i.e. after
            # activation, before any post-panel edit (matches the real flow).
            (td / "judge.md").write_text(
                judge_md.format(fp=tree_state_fingerprint(d)), encoding="utf-8")
        if change_after_panel:
            (d / "code.py").write_text("x = 2\n", encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "done"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Task 001 done.", r.stdout)
        return r.stdout

    def test_post_panel_code_change_prints_the_advisory(self):
        # The 010 scenario: impl panel stamped, then the code changed.
        out = self._close(judge_md=impl_round("{fp}"), change_after_panel=True)
        self.assertIn(ADVISORY_MARKER, out)
        self.assertIn("panel-review", out)  # names the remedy

    def test_matching_fingerprint_stays_silent(self):
        # Negative control: an advisory that fires on every close trains
        # people to ignore it (the cries-wolf class).
        out = self._close(judge_md=impl_round("{fp}"), change_after_panel=False)
        self.assertNotIn(ADVISORY_MARKER, out)

    def test_no_impl_round_stays_silent_and_close_unharmed(self):
        plan_only = ("# Panel Plan Review — task 1\n\n"
                     "**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
                     f"**Commit:** {'a' * 40}\n"
                     "**Tree-state:** {fp}\n\nfindings body\n")
        out = self._close(judge_md=plan_only, change_after_panel=True)
        self.assertNotIn(ADVISORY_MARKER, out)


if __name__ == "__main__":
    unittest.main()
