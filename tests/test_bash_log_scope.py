#!/usr/bin/env python3
"""bash-log.sh logs what the agent ran — not every loop iteration, not the statusline (PLAN S5b).

The DEBUG trap fires for every simple command of every non-interactive bash in a
playbook project. On 2026-09-21 that meant 195,168 x 3 rows of loop iterations and
~40 statusline assignments per render (~710 renders a day), and the history reached
136 MB. The logger now writes each distinct command text once per shell process,
installs no trap in a shell that exports PLAYBOOK_NO_BASHLOG=1 or runs a script named
statusline, and rotates a history past 50 MB. Every probe runs the REAL bash-log.sh
through BASH_ENV, the way the harness does.

Run: python3 -m unittest tests.test_bash_log_scope
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._bashcheck import bash_or_skip

BL = Path(__file__).resolve().parent.parent / "plugins" / "playbook" / "scripts" / "bash-log.sh"


class BashLogScope(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".agent" / "tasks").mkdir(parents=True)
        self.hist = self.proj / ".agent" / "bash_history"

    def _bash(self, script, extra_env=None, argv=None):
        env = dict(os.environ, BASH_ENV=str(BL))
        env.pop("PLAYBOOK_NO_BASHLOG", None)
        env.update(extra_env or {})
        cmd = argv or [bash_or_skip(), "-c", script]
        return subprocess.run(cmd, cwd=self.proj, env=env, capture_output=True, text=True, timeout=60)

    def _lines(self):
        return self.hist.read_text(encoding="utf-8", errors="replace").splitlines() if self.hist.exists() else []

    def test_a_loop_logs_each_command_text_once(self):
        self._bash("for i in 1 2 3 4 5; do echo $i >/dev/null; done; echo done >/dev/null")
        cmds = [ln.split(" | AGENT | ", 1)[1] for ln in self._lines()]
        self.assertLessEqual(len(cmds), 3, cmds)
        self.assertIn("echo done > /dev/null", cmds)

    def test_distinct_commands_are_all_logged(self):
        self._bash("echo one >/dev/null; echo two >/dev/null; echo three >/dev/null")
        text = "\n".join(self._lines())
        for w in ("one", "two", "three"):
            self.assertIn(f"echo {w}", text)

    def test_a_shell_that_opts_out_logs_nothing(self):
        self._bash("echo statusline-shaped >/dev/null", {"PLAYBOOK_NO_BASHLOG": "1"})
        self.assertNotIn("statusline-shaped", "\n".join(self._lines()))
        self._bash("echo opted-in >/dev/null", {"PLAYBOOK_NO_BASHLOG": "0"})
        self.assertIn("opted-in", "\n".join(self._lines()))

    def test_a_script_named_statusline_is_not_logged(self):
        script = self.proj / "statusline.sh"
        script.write_text("#!/bin/bash\necho by-name >/dev/null\n", encoding="utf-8")
        script.chmod(0o755)
        self._bash("", argv=[bash_or_skip(), str(script)])
        self.assertNotIn("by-name", "\n".join(self._lines()))

    def test_a_history_past_50_mb_is_rotated(self):
        with open(self.hist, "wb") as fh:
            fh.truncate(51 * 1024 * 1024)
        self._bash("echo rotate >/dev/null")
        archived = [p.name for p in self.hist.parent.iterdir() if p.name.startswith("bash_history.archived-")]
        self.assertEqual(len(archived), 1, archived)
        self.assertLess(self.hist.stat().st_size, 1024 * 1024)
        self.assertIn("echo rotate", self.hist.read_text(encoding="utf-8", errors="replace"))

    def test_a_small_history_is_not_rotated(self):
        self.hist.write_text("2026-09-24 00:00:00 | AGENT | echo old\n", encoding="utf-8")
        self._bash("echo new >/dev/null")
        self.assertFalse([p for p in self.hist.parent.iterdir() if p.name.startswith("bash_history.archived-")])
        self.assertIn("echo old", self.hist.read_text(encoding="utf-8"))

    def test_a_set_e_shell_survives_the_trap(self):
        r = self._bash("set -e; [ -d /nonexistent ] || true; false || true; echo still-alive")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("still-alive", r.stdout)


if __name__ == "__main__":
    unittest.main()
