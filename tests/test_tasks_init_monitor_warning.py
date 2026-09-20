#!/usr/bin/env python3
"""Task 073 finding C8: `tasks init` warned that `.claude/settings.json`'s hook
registrations and `.claude/hooks/monitor-nudge.sh` are "stale copies … remove
them" — but `scripts/init` (the /playbook:init path) CREATES exactly those for
the monitor nudge. Following the advice disables the monitor. The sanctioned
monitor-nudge registration + file must not trigger the warning; anything else
still does."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"


class TasksInitMonitorWarning(unittest.TestCase):
    def _run_init(self, d):
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-init073")
        return subprocess.run([sys.executable, "-m", "tasks.cli", "init"], cwd=d, env=env,
                              capture_output=True, text=True, timeout=60)

    def _project(self, settings_hooks, hook_files):
        d = Path(tempfile.mkdtemp())
        (d / ".claude" / "hooks").mkdir(parents=True)
        (d / ".claude" / "settings.json").write_text(json.dumps(
            {"permissions": {"deny": []}, "hooks": settings_hooks}), encoding="utf-8")
        for name in hook_files:
            (d / ".claude" / "hooks" / name).write_text("#!/bin/bash\n", encoding="utf-8")
        return d

    def test_sanctioned_monitor_nudge_is_not_reported_as_stale(self):
        d = self._project({"PostToolUse": [{"matcher": "", "hooks": [{"type": "command",
                              "command": "bash \"$CLAUDE_PROJECT_DIR/.claude/hooks/monitor-nudge.sh\""}]}]},
                          ["monitor-nudge.sh"])
        r = self._run_init(d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("stale copies", r.stdout)
        self.assertNotIn("duplicate plugin hooks", r.stdout)

    def test_foreign_hook_copies_are_still_reported(self):
        d = self._project({"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                              "command": "bash .claude/hooks/task-gate-hook"}]}]},
                          ["monitor-nudge.sh", "task-gate-hook"])
        r = self._run_init(d)
        self.assertIn("duplicate plugin hooks", r.stdout)
        self.assertIn("stale copies", r.stdout)
        self.assertIn("task-gate-hook", r.stdout)

    def test_non_object_settings_json_does_not_crash(self):
        # impl-panel round 1 (codex-medium): `[]` is valid JSON; `.get()` on it raised.
        d = Path(tempfile.mkdtemp())
        (d / ".claude").mkdir()
        (d / ".claude" / "settings.json").write_text("[]", encoding="utf-8")
        r = self._run_init(d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_nested_settings_shapes_do_not_crash(self):
        # impl-panel round 2 (codex ×2): every container level may be malformed
        for shape in ('{"hooks": ["x"]}', '{"hooks": {"PostToolUse": "x"}}',
                      '{"hooks": {"PostToolUse": [{"hooks": ["bad"]}]}}',
                      '{"hooks": {"PostToolUse": [{"hooks": [{"command": 5}]}]}}', '{"hooks": null}'):
            with self.subTest(shape=shape):
                d = Path(tempfile.mkdtemp())
                (d / ".claude").mkdir()
                (d / ".claude" / "settings.json").write_text(shape, encoding="utf-8")
                r = self._run_init(d)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
