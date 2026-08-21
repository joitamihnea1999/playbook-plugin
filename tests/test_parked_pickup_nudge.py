#!/usr/bin/env python3
"""F21 — activation nudges the parked-consumption marker (batch-6 finding).

Task 012 genuinely consumed task 010's parked item (the designed pickup!) but
010's Parked entry was never marked `[promoted → 012]`, so the lifecycle still
shows it open. The markers were taught only at CLOSE (own-task items); the
consumption moment had no nudge. `tasks work <N>` now prints one line when
open parked items exist in EARLIER tasks — and stays silent when none do.

Run: python3 -m unittest tests.test_parked_pickup_nudge
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

ENV = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-f21")


def _project(parked_in_001: bool) -> Path:
    proj = Path(tempfile.mkdtemp())
    t1 = proj / ".agent" / "tasks" / "001-old"
    t1.mkdir(parents=True)
    parked = ("## Parked\n- **Thread-safe guard.** Needs a lock.\n"
              if parked_in_001 else
              "## Parked\n- **Thread-safe guard.** [promoted → 002]\n")
    (t1 / "task.md").write_text(
        "# 001 - Old\n\n## Status\ndone\n\n" + parked + "\n---\n",
        encoding="utf-8")
    t2 = proj / ".agent" / "tasks" / "002-new"
    t2.mkdir(parents=True)
    (t2 / "task.md").write_text(
        "# 002 - New\n\n## Status\npending\n\n## Work Plan\n- [ ] G1: do\n",
        encoding="utf-8")
    return proj


def _activate(proj: Path):
    return subprocess.run([sys.executable, "-m", "tasks.cli", "work", "2"],
                          cwd=proj, env=ENV, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=60)


class ParkedPickupNudge(unittest.TestCase):
    def test_open_parked_elsewhere_nudges_with_marker(self):
        r = _activate(_project(parked_in_001=True))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("open parked item(s) in earlier tasks", r.stdout)
        self.assertIn("[promoted → 002]", r.stdout,
                      "the nudge must teach the marker with THIS task's number")

    def test_all_resolved_stays_silent(self):
        # Negative control: a nudge that fires on every activation forever
        # gets ignored (the F16 lesson) — resolved items must not count.
        r = _activate(_project(parked_in_001=False))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("open parked item(s) in earlier tasks", r.stdout)


if __name__ == "__main__":
    unittest.main()
