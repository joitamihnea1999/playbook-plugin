"""NEW-2 (1.5.11 audit): the enforcing task-gate must not fail OPEN when the
sessions dir can't be created.

`mkdir -p "$SESSION_DIR"` ran unguarded under `set -e` BEFORE the block
decision, so an unwritable/full (ENOSPC, read-only, wrong-perms) sessions dir
aborted the hook with exit 1 — a NON-blocking code — and the code edit
proceeded. Enforcement stopped globally. The non-enforcing state-echo hook
already guarded the identical call; the enforcing gate did not.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "plugins" / "playbook" / "scripts" / "task-gate-hook"


@unittest.skipIf(os.geteuid() == 0, "chmod cannot restrict root")
class GateMkdirFailOpen(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)
        self.sessions = self.project / ".agent" / "sessions"
        self.sessions.mkdir()
        os.chmod(self.sessions, 0o500)  # r-x: cannot create the session subdir
        self.addCleanup(lambda: os.chmod(self.sessions, stat.S_IRWXU))

    def test_gate_blocks_when_sessions_dir_unwritable(self):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = "pid-mkdirfail"
        env.pop("BASH_ENV", None)
        r = subprocess.run(
            ["bash", str(HOOK)], cwd=self.project, env=env, text=True,
            input=json.dumps({"tool_name": "Edit",
                              "tool_input": {"file_path": str(self.project / "src" / "main.py")}}),
            capture_output=True)
        self.assertEqual(r.returncode, 2,
                         f"enforcing gate failed OPEN on unwritable sessions dir "
                         f"(rc={r.returncode}); stderr={r.stderr}")


if __name__ == "__main__":
    unittest.main()
