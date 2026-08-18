"""I1 (verification-report-1.5.9): the ENFORCING gate and Stop hook must not
fail OPEN on a malformed `.agent/current_user`.

`resolve_agent_dir` (gate-echo-lib.sh) calls `exit 1` on an invalid marker;
`task-gate-hook` / `stop-hook` invoke it at top level under `set -e`, so the
whole hook aborts with exit 1 — which Claude Code treats as a NON-blocking
error, so the code edit / the stop proceeds. Trivially reachable:
`echo alice@evil > .agent/current_user` (a `@` username) disabled the gate.

The fix makes both enforcing hooks FAIL CLOSED (block, exit 2) on a malformed
marker — never abort-open — while still allowing `.agent/`/`.claude/` edits so
the marker stays fixable, and (stop-hook) relying on the stop_hook_active valve
so a re-issued stop still ends the turn (no wedge).
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


class MarkerFailClosedBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        # Multi-user layout: a lane dir + a marker.
        (self.project / ".agent" / "alice" / "tasks" / "001-x").mkdir(parents=True)
        (self.project / ".agent" / "alice" / "tasks" / "001-x" / "task.md").write_text(
            "# 001 - x\n## Status\nin_progress\n## Work Plan\n- [ ] open gate\n",
            encoding="utf-8")

    def set_marker(self, content):
        (self.project / ".agent" / "current_user").write_text(content, encoding="utf-8")

    def run_hook(self, hook, stdin_obj):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = "pid-marker-test"
        return subprocess.run(
            ["bash", str(SCRIPTS / hook)],
            cwd=self.project, env=env, text=True,
            input=json.dumps(stdin_obj), capture_output=True,
        )


class TaskGateFailClosed(MarkerFailClosedBase):
    def test_code_edit_blocked_on_malformed_marker(self):
        self.set_marker("alice@evil\n")  # '@' is not a valid username char
        r = self.run_hook("task-gate-hook", {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(self.project / "src" / "main.py")},
        })
        self.assertEqual(r.returncode, 2,
                         f"malformed marker did not fail closed (rc={r.returncode}): "
                         f"{r.stderr}")

    def test_agent_edit_allowed_on_malformed_marker(self):
        # Control: the marker itself must stay editable so it's fixable.
        self.set_marker("alice@evil\n")
        r = self.run_hook("task-gate-hook", {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(self.project / ".agent" / "current_user")},
        })
        self.assertEqual(r.returncode, 0,
                         f"could not edit the marker to fix it: {r.stderr}")

    def test_valid_marker_no_task_still_blocks(self):
        # Negative control: a VALID marker with no active task still blocks a
        # code edit (unchanged behavior).
        self.set_marker("alice\n")
        r = self.run_hook("task-gate-hook", {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(self.project / "src" / "main.py")},
        })
        self.assertEqual(r.returncode, 2, f"valid-marker gate regressed: {r.stderr}")


class StopHookFailClosed(MarkerFailClosedBase):
    def test_stop_blocked_on_malformed_marker(self):
        self.set_marker("alice@evil\n")
        r = self.run_hook("stop-hook", {"stop_hook_active": False})
        self.assertEqual(r.returncode, 2,
                         f"stop allowed on malformed marker (rc={r.returncode}): "
                         f"{r.stderr}")

    def test_stop_valve_still_allows_second_stop(self):
        # The stop_hook_active valve must still end the turn so a malformed
        # marker can't wedge the session.
        self.set_marker("alice@evil\n")
        r = self.run_hook("stop-hook", {"stop_hook_active": True})
        self.assertEqual(r.returncode, 0,
                         f"stop valve broken (rc={r.returncode}): {r.stderr}")


if __name__ == "__main__":
    unittest.main()
