"""B1 + D2 (1.5.11 audit): the `light` task type is first-class and must behave
like the others.

B1: `tasks new light <name> <intent>` silently dropped the intent — `create_task`
substituted intent only into the feature/quick placeholders, not the light
template's `(one line — what to do and what proves it worked)`.
D2: `--help` "Task types:" omitted `light` (usage_text built the list without it).
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


class LightType(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)

    def run_tasks(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["PLAYBOOK_SESSION_ID"] = "pid-light-test"
        env.pop("BASH_ENV", None)
        return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                              cwd=self.project, env=env, text=True, capture_output=True)

    def test_new_light_prefills_intent(self):
        r = self.run_tasks("new", "light", "fix-thing", "make the widget stop flickering")
        self.assertEqual(r.returncode, 0, f"new light failed: {r.stderr}")
        tdir = next((self.project / ".agent" / "tasks").glob("*-fix-thing"))
        body = (tdir / "task.md").read_text(encoding="utf-8")
        self.assertIn("make the widget stop flickering", body,
                      "light intent was dropped (B1)")
        self.assertNotIn("(one line — what to do and what proves it worked)", body,
                         "light Intent placeholder was left unsubstituted (B1)")

    def test_help_lists_light_type(self):
        r = self.run_tasks("--help")
        out = r.stdout + r.stderr
        # The "Task types:" line must include light.
        self.assertRegex(out, r"Task types:[^\n]*\blight\b",
                         f"--help Task types omits 'light' (D2):\n{out}")


if __name__ == "__main__":
    unittest.main()
