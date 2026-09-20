"""B2 (1.5.11 audit): `tasks freehand log` must work on the plugin's declared
Python 3.10+ floor, not only 3.11+.

The freehand writer emits a `Z`-suffixed UTC timestamp, but the reader parsed it
with `datetime.fromisoformat()`, which rejects a trailing `Z` before Python 3.11.
On Ubuntu 22.04 (Python 3.10 — a very common host) `tasks freehand log` crashed
with "cannot parse freehand-start timestamp" before it did anything. This test is
red-first on 3.10 and a regression guard everywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"
SID = "pid-freehand-test"


class FreehandLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)

    def run_tasks(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["PLAYBOOK_SESSION_ID"] = SID
        env.pop("BASH_ENV", None)
        return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                              cwd=self.project, env=env, text=True, capture_output=True)

    def test_freehand_log_parses_the_z_timestamp(self):
        c = self.run_tasks("freehand", "fix-thing")
        self.assertEqual(c.returncode, 0, f"freehand create failed: {c.stderr}")
        # freehand log reads chat_log.md — provide one with a recent entry so the
        # command runs to completion (the bug was the timestamp parse, before this).
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        (self.project / ".agent" / "chat_log.md").write_text(
            f"# Project Chat Log\n\n---\n\n**[M001]** [{now}] `HOST` (claude/{SID})\n\n"
            "did some freehand work\n", encoding="utf-8")
        r = self.run_tasks("freehand", "log")
        self.assertNotIn("cannot parse freehand-start timestamp", r.stderr,
                         "freehand log crashed on the Z timestamp (B2)")
        self.assertNotIn("Traceback", r.stderr, r.stderr)
        self.assertEqual(r.returncode, 0, f"freehand log failed: {r.stderr}")


if __name__ == "__main__":
    unittest.main()


class GauntletFreehandPlacement(unittest.TestCase):
    """Task 073 finding B8: `tasks freehand` on a LIGHT task (which has `## Work`,
    not `## Work Plan`) appended its `### Freehand` block at EOF — inside the
    last H2, `## Parked` — so `tasks parked` and the close's parked-debt warning
    listed the four Freehand gates as parked items (even when checked)."""

    def test_freehand_block_never_lands_inside_parked(self):
        import subprocess, sys, os
        sys.path.insert(0, str(PLUGIN))
        from tasks.core import open_parked_items
        d = Path(tempfile.mkdtemp())
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-fh073")
        def run(*a):
            return subprocess.run([sys.executable, "-m", "tasks.cli", *a], cwd=d, env=env,
                                  capture_output=True, text=True, timeout=60)
        run("init")
        self.assertEqual(run("new", "light", "lt", "light task").returncode, 0)
        self.assertEqual(run("work", "1").returncode, 0)
        self.assertEqual(run("freehand").returncode, 0)
        tf = next((d / ".agent" / "tasks").glob("001-*/task.md"))
        text = tf.read_text(encoding="utf-8")
        self.assertIn("### Freehand", text)
        self.assertLess(text.index("### Freehand"), text.index("## Parked"),
                        "freehand block must sit before ## Parked, not inside it")
        self.assertEqual(open_parked_items(text), [],
                         "the Freehand gates must not read as parked debt")
