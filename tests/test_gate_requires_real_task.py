"""I2 (verification-report-1.5.9): the code-edit gate must resolve
`current_state` to a REAL task, not merely check it is non-empty.

`task-gate-hook` allowed a code edit whenever the pointer's first line was
non-empty, without resolving it to a task dir. `.agent/**` writes are exempt
from the gate, so with no active task an agent can
`Write .agent/sessions/<id>/current_state=junk` (exit 0) then edit any code file
(exit 0) — defeating the "no code without an active task" promise. It also fires
on stale/leftover pointers whose task dir was renamed or deleted.

Fix: the pointer must resolve to an existing `NNN-*/task.md`, else the edit is
blocked.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "playbook" / "scripts"
SID = "pid-gatetest"


class GateRequiresRealTask(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        d = self.project / ".agent" / "tasks" / "001-real"
        d.mkdir(parents=True)
        (d / "task.md").write_text(
            "# 001 - real\n## Status\nin_progress\n## Work Plan\n- [ ] g\n",
            encoding="utf-8")
        self.pointer = self.project / ".agent" / "sessions" / SID / "current_state"
        self.pointer.parent.mkdir(parents=True)

    def run_gate(self, file_path):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = SID
        return subprocess.run(
            ["bash", str(SCRIPTS / "task-gate-hook")],
            cwd=self.project, env=env, text=True,
            input=json.dumps({"tool_name": "Edit",
                              "tool_input": {"file_path": str(file_path)}}),
            capture_output=True,
        )

    def test_junk_pointer_does_not_authorize_code_edit(self):
        self.pointer.write_text("junk\n", encoding="utf-8")
        r = self.run_gate(self.project / "src" / "main.py")
        self.assertEqual(r.returncode, 2,
                         f"junk pointer self-authorized a code edit (rc={r.returncode})")

    def test_stale_number_pointer_does_not_authorize(self):
        # A number whose task dir does not exist (renamed/deleted).
        self.pointer.write_text("999\n", encoding="utf-8")
        r = self.run_gate(self.project / "src" / "main.py")
        self.assertEqual(r.returncode, 2,
                         f"stale pointer self-authorized a code edit (rc={r.returncode})")

    def test_glob_metachar_pointer_does_not_authorize(self):
        # N2 (verification-report-1.5.10): the pointer feeds a `find -path
        # "*/${TASK}-*/*"` glob, so a bare `*` matched the real task 001-real
        # and self-authorized. A glob metacharacter is not a task number.
        for meta in ("*", "?", "[0-9]"):
            with self.subTest(pointer=meta):
                self.pointer.write_text(meta + "\n", encoding="utf-8")
                r = self.run_gate(self.project / "src" / "main.py")
                self.assertEqual(r.returncode, 2,
                                 f"glob pointer {meta!r} self-authorized (rc={r.returncode})")

    def test_real_pointer_authorizes(self):
        # Negative control: a pointer resolving to a real task still allows.
        self.pointer.write_text("001\n", encoding="utf-8")
        r = self.run_gate(self.project / "src" / "main.py")
        self.assertEqual(r.returncode, 0,
                         f"real active task was blocked: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
