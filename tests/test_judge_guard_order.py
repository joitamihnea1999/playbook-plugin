"""I3 (verification-report-1.5.9): the judge-session Bash guard must run BEFORE
the session-injection early-exit, or it is dead code.

task-gate-hook's injection branch matches `tasks (work|...|new)` and exits 0
*before* Guard 3 (block a judge session from creating/activating tasks). So
`PLAYBOOK_SESSION_ID=judge` + `tasks new feature sneaky` exited 0 with an
`allow` decision, never reaching the block.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "plugins" / "playbook" / "scripts" / "task-gate-hook"


class JudgeGuardOrder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)

    def run_gate(self, cmd, session_id):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = session_id
        return subprocess.run(
            ["bash", str(HOOK)],
            cwd=self.project, env=env, text=True,
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": cmd}}),
            capture_output=True,
        )

    def test_judge_new_task_is_blocked(self):
        r = self.run_gate("tasks new feature sneaky", "judge")
        self.assertEqual(r.returncode, 2,
                         f"judge session created a task (rc={r.returncode}): {r.stdout}")
        self.assertIn("Judge session", r.stderr)

    def test_judge_work_task_is_blocked(self):
        r = self.run_gate("tasks work 001", "judge")
        self.assertEqual(r.returncode, 2,
                         f"judge session activated a task (rc={r.returncode}): {r.stdout}")

    def test_normal_session_new_task_is_injected(self):
        # Negative control: a normal session's `tasks new` is injected + allowed.
        r = self.run_gate("tasks new feature ok", "pid-normal")
        self.assertEqual(r.returncode, 0, f"normal tasks new blocked: {r.stderr}")
        self.assertIn("permissionDecision", r.stdout)

    def test_judge_status_still_allowed(self):
        # A judge may read status — only new/work are blocked.
        r = self.run_gate("tasks status", "judge")
        self.assertEqual(r.returncode, 0, f"judge status blocked: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
