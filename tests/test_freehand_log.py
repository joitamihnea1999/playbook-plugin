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
