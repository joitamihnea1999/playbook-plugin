"""I5 (verification-report-1.5.9): `tasks doctor` must exit non-zero when any
check FAILs, so `tasks doctor && deploy` (or a CI gate) can't get a false green.

`cmd_doctor` was dispatched with no `sys.exit` wrapping and never returned a
status, so a project missing CLAUDE.md/MIND_MAP printed FAIL lines and exited 0.
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


class DoctorExitCode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        self.project.mkdir()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)

    def _doctor(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["HOME"] = str(self.home)
        env["PLAYBOOK_SESSION_ID"] = "pid-doctortest"
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", "doctor"],
            cwd=self.project, env=env, text=True, capture_output=True,
        )

    def test_failing_project_exits_nonzero(self):
        # Bare project: tasks/, CLAUDE.md, MIND_MAP.md all missing → FAILs.
        r = self._doctor()
        self.assertIn("FAIL", r.stdout, "doctor printed no FAIL in a bare project")
        self.assertNotEqual(r.returncode, 0,
                            "doctor exited 0 despite FAIL lines (I5)")

    def test_healthy_project_exits_zero(self):
        # Negative control: with the project files present, doctor reports no
        # failures and exits 0 (so the fix isn't a hardcoded non-zero).
        (self.project / ".agent" / "tasks").mkdir(parents=True)
        (self.project / "CLAUDE.md").write_text("# proj\n", encoding="utf-8")
        (self.project / "MIND_MAP.md").write_text("# MIND_MAP\n", encoding="utf-8")
        r = self._doctor()
        self.assertEqual(r.returncode, 0,
                         f"healthy doctor did not exit 0: {r.stdout}\n{r.stderr}")


if __name__ == "__main__":
    unittest.main()
