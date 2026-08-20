#!/usr/bin/env python3
"""Inspection/help paths must not cross a side-effecting branch."""
from __future__ import annotations

import os
import subprocess
from tests._bashcheck import bash_or_skip
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent / "plugins/playbook"
SCRIPTS = PLUGIN / "scripts"


class TasksHelpIsDry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / ".agent/tasks").mkdir(parents=True)
        self.env = dict(os.environ, PYTHONPATH=str(PLUGIN),
                        PLAYBOOK_SESSION_ID="pid-help-dry")
        self.env.pop("BASH_ENV", None)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", *args], cwd=self.project,
            env=self.env, capture_output=True, text=True, timeout=30,
        )

    def task(self, body: str) -> Path:
        d = self.project / ".agent/tasks/001-t"
        d.mkdir(exist_ok=True)
        p = d / "task.md"
        p.write_text(body, encoding="utf-8")
        return p

    def test_work_done_help_does_not_close(self):
        p = self.task(
            "# 001\n\n## Status\npending\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1: done\n")
        self.assertEqual(self.cli("work", "1").returncode, 0)
        before = p.read_bytes()
        r = self.cli("work", "done", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(p.read_bytes(), before)
        self.assertNotIn("Task 001 done.", r.stdout)

    def test_new_compact_and_audit_help_do_not_write(self):
        r = self.cli("new", "light", "demo", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(list((self.project / ".agent/tasks").iterdir()), [])

        p = self.task(
            "# 001\n\n<!-- archive:start -->\nold\n<!-- archive:end -->\n")
        before = p.read_bytes()
        self.assertEqual(self.cli("compact", "1", "--help").returncode, 0)
        self.assertEqual(p.read_bytes(), before)
        self.assertFalse((p.parent / "task-archive.md").exists())

        self.assertEqual(self.cli("audit", "--help").returncode, 0)
        self.assertEqual(p.read_bytes(), before)
        self.assertNotIn("Pre-Panel Audit", p.read_text(encoding="utf-8"))

    def test_subcommand_help_runs_before_session_gc(self):
        session = self.project / ".agent/sessions/pid-999999"
        session.mkdir(parents=True)
        state = session / "current_state"
        state.write_text("001\n", encoding="utf-8")
        old = time.time() - 10 * 86400
        os.utime(state, (old, old))
        r = self.cli("merge-doctor", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(session.exists(), "help ran session GC")


class LauncherHelpIsDry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        (self.project / ".agent/tasks").mkdir(parents=True)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        for name in ("codex", "grok", "agy"):
            fake = self.bindir / name
            fake.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            fake.chmod(0o755)

    def test_provider_launcher_help_neither_provisions_nor_executes(self):
        env = dict(os.environ, PATH=f"{self.bindir}:/usr/bin:/bin")
        env.pop("BASH_ENV", None)
        for name in ("codex", "grok", "agy"):
            with self.subTest(provider=name):
                r = subprocess.run(
                    [bash_or_skip(), str(SCRIPTS / f"playbook-{name}"), "--help"],
                    cwd=self.project, env=env, capture_output=True, text=True,
                    timeout=30,
                )
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("Usage:", r.stdout)
                sessions = self.project / ".agent/sessions"
                self.assertFalse(sessions.exists() and any(sessions.iterdir()))


class BashLogSkipsHookInternals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        # A root `.agent/tasks/` is what makes the root a lane: without it the
        # logger has no lane it can prove it owns and skips (PB-LANE-RESOLUTION),
        # which would make the agent-shell half of this test vacuous.
        (self.project / ".agent/tasks").mkdir(parents=True)
        self.history = self.project / ".agent/bash_history"

    def test_hook_shell_is_not_logged_but_agent_shell_is(self):
        hook = Path(self.tmp.name) / "probe-hook"
        hook.write_text("#!/bin/bash\necho hook-ran\n", encoding="utf-8")
        hook.chmod(0o755)
        env = dict(os.environ, BASH_ENV=str(SCRIPTS / "bash-log.sh"))
        r = subprocess.run([bash_or_skip(), str(hook)], cwd=self.project, env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.history.exists(), "hook internals polluted history")

        r = subprocess.run([bash_or_skip(), "-c", "echo agent-ran"], cwd=self.project,
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.history.exists(), "real Bash tool shell was not logged")
        self.assertIn("echo agent-ran", self.history.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
