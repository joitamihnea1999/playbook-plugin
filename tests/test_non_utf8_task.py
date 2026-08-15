"""I10 (verification-report-1.5.9): the open/close path must not CRASH on a
non-UTF-8 task.md.

The lifecycle read task.md with `read_text(encoding="utf-8")` and no `errors=`;
a `UnicodeDecodeError` is a `ValueError`, so the surrounding `except OSError`
misses it. One `0xE9` byte (a cp1252 `é`, a real field scenario) made the task
UNOPENABLE (`tasks work N`) and UNCLOSEABLE (`tasks work done`) with a raw
traceback. The fix reads leniently (errors="replace") so the task stays usable.
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
SID = "pid-utf8-test"

# A task.md with a raw 0xE9 (cp1252 'é') byte — NOT valid UTF-8.
TASK_BYTES = (
    b"# 001 - x\n\n## Status\nin_progress\n\n## Risk\nreversible\n\n"
    b"## Work Plan\n- [x] caf\xe9 gate done\n"
)


class NonUtf8Task(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        d = self.project / ".agent" / "tasks" / "001-x"
        d.mkdir(parents=True)
        (d / "task.md").write_bytes(TASK_BYTES)

    def run_tasks(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["PLAYBOOK_SESSION_ID"] = SID
        env.pop("BASH_ENV", None)
        return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                              cwd=self.project, env=env, text=True,
                              capture_output=True)

    def test_activate_does_not_crash(self):
        r = self.run_tasks("work", "001")
        self.assertNotIn("UnicodeDecodeError", r.stderr, r.stderr)
        self.assertNotIn("Traceback", r.stderr, r.stderr)
        self.assertEqual(r.returncode, 0, f"activation crashed: {r.stderr}")

    def test_close_does_not_crash(self):
        self.run_tasks("work", "001")
        r = self.run_tasks("work", "done")
        self.assertNotIn("UnicodeDecodeError", r.stderr, r.stderr)
        self.assertNotIn("Traceback", r.stderr, r.stderr)
        self.assertEqual(r.returncode, 0, f"close crashed: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
