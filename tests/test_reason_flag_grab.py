"""I11 (verification-report-1.5.9): `--reason` must not swallow the next FLAG.

`lifecycle.cmd_work` took `cmd_args[i+1]` after `--reason` unconditionally, so
`tasks work done --reason --force` force-closed the task with the reason
literally `"--force"` — the owner-decreed "a forced close must record why" was
satisfied by a flag name.
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
SID = "pid-reason-test"

TASK_MD = """# 001 - x

## Status
in_progress

## Risk
reversible

## Work Plan
- [ ] an OPEN gate (forces --force to close)
"""


class ReasonFlagGrab(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        d = self.project / ".agent" / "tasks" / "001-x"
        d.mkdir(parents=True)
        self.tf = d / "task.md"
        self.tf.write_text(TASK_MD, encoding="utf-8")
        p = self.project / ".agent" / "sessions" / SID / "current_state"
        p.parent.mkdir(parents=True)
        p.write_text("001\n", encoding="utf-8")

    def run_tasks(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["PLAYBOOK_SESSION_ID"] = SID
        env.pop("BASH_ENV", None)
        return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                              cwd=self.project, env=env, text=True,
                              capture_output=True)

    def _status(self):
        for i, ln in enumerate(self.tf.read_text(encoding="utf-8").splitlines()):
            if ln.strip() == "## Status":
                return self.tf.read_text(encoding="utf-8").splitlines()[i + 1].strip()
        return ""

    def test_reason_does_not_grab_force_flag(self):
        r = self.run_tasks("work", "done", "--reason", "--force")
        # The forced close has NO real reason → it must be refused, not closed
        # with reason "--force".
        self.assertNotEqual(r.returncode, 0,
                            "force-closed with a flag as the reason (I11)")
        self.assertNotEqual(self._status(), "done",
                            "task closed with '--force' recorded as the reason")

    def test_real_reason_still_force_closes(self):
        # Negative control: a genuine reason still force-closes.
        r = self.run_tasks("work", "done", "--force", "--reason", "manual override, verified by hand")
        self.assertEqual(r.returncode, 0, f"legit force-close refused: {r.stderr}")
        self.assertEqual(self._status(), "done")


if __name__ == "__main__":
    unittest.main()
