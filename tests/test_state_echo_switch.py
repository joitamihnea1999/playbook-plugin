#!/usr/bin/env python3
"""Task 073 finding C10: state-echo-hook logs the PREVIOUS gate into chat_log.md
as `- [x] <text>` on ANY gate-key change — including a TASK SWITCH where that
gate was never completed. Retro attribution consumes these G-entries; a false
`[x]` is a false record. The switch entry must stay (it anchors the previous
task's window) but must not claim completion."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
SCRIPTS = PLUGIN / "scripts"


class StateEchoTaskSwitch(unittest.TestCase):
    def test_switching_tasks_does_not_log_the_open_gate_as_done(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not available")
        d = Path(tempfile.mkdtemp())
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-sw073",
                   CLAUDE_PLUGIN_ROOT=str(PLUGIN))
        def run(*a):
            return subprocess.run([sys.executable, "-m", "tasks.cli", *a], cwd=d, env=env,
                                  capture_output=True, text=True, timeout=60)
        run("init")
        self.assertEqual(run("new", "quick", "one", "first").returncode, 0)
        self.assertEqual(run("new", "quick", "two", "second").returncode, 0)
        payload = '{"tool_name":"Bash","tool_input":{"command":"ls"},"tool_response":{}}'
        def echo():
            return subprocess.run([bash, str(SCRIPTS / "state-echo-hook")], input=payload,
                                  cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run("work", "1").returncode, 0)
        echo()
        self.assertEqual(run("work", "2", "--force").returncode, 0)
        second = echo()
        # impl-panel round 1 (codex-medium / grok): the hook's own echo must not
        # tell the session the gate completed either.
        self.assertNotIn("previous gate done", second.stdout)
        log = (d / ".agent" / "chat_log.md").read_text(encoding="utf-8")
        self.assertIn("**[G001:", log, "the switch must still leave a G-entry for task 001")
        g_block = log[log.index("**[G001:"):]
        self.assertNotIn("- [x] Do the work", g_block,
                         "an OPEN gate was logged as completed on a task switch")
        self.assertIn("Do the work", g_block)


if __name__ == "__main__":
    unittest.main()
