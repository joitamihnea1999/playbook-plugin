#!/usr/bin/env python3
"""C1 — `tasks handoff`: codify the proven manual session-handoff pattern.

`tasks handoff` writes the mechanical ~80% of a handoff (project + nested-code-root
git state, gate progress, the latest verification receipt line, a timestamp) into
the ACTIVE task's `## Handoff` section, prints instructions for the agent to append
the judgment ~20%, and puts the task into the honest blocked state (reason
"handoff", reusing `tasks blocked` semantics — never a faked checkbox). A fresh
`tasks bootstrap` surfaces an unconsumed handoff prominently; resuming with
`tasks work <N>` consumes it (the section stays as history).

Run: python3 -m unittest tests.test_handoff
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"


def _git(d, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=d, check=True, capture_output=True)


def _env(session="pid-handoff"):
    return dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID=session)


def _cli(d, *args, session="pid-handoff"):
    return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                          cwd=d, env=_env(session), capture_output=True,
                          text=True, timeout=60)


class _Base(unittest.TestCase):
    def _project(self, *, code_root=False, receipt=True, git=True):
        d = Path(tempfile.mkdtemp())
        if git:
            subprocess.run(["git", "init", "-q"], cwd=d, check=True)
            (d / "code.py").write_text("x = 1\n", encoding="utf-8")
            _git(d, "add", "-A")
            _git(d, "commit", "-qm", "seed")
        (d / ".agent").mkdir(exist_ok=True)
        cfg = {}
        if code_root:
            (d / ".gitignore").write_text("sub/\n", encoding="utf-8")
            if git:
                _git(d, "add", "-A")
                _git(d, "commit", "-qm", "ignore sub")
            sub = d / "sub"
            sub.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=sub, check=True)
            (sub / "app.py").write_text("y = 1\n", encoding="utf-8")
            _git(sub, "add", "-A")
            _git(sub, "commit", "-qm", "seed nested")
            cfg["code_roots"] = ["sub"]
        if cfg:
            import json
            (d / ".agent" / "config.json").write_text(json.dumps(cfg),
                                                       encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-demo"
        td.mkdir(parents=True)
        receipt_block = ""
        if receipt:
            receipt_block = (
                "\n## Verification Receipt\n"
                "### 2026-08-25T10:00:00+00:00 · risk reversible · commit abc1234\n"
                "- **Commands:**\n    - [PASS] `python3 scripts/verify` (config)\n")
        (td / "task.md").write_text(
            "# 001 - Demo\n\n## Status\npending\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1: first thing\n- [ ] G2: the next thing\n"
            "- [ ] G3: after that\n" + receipt_block, encoding="utf-8")
        r = _cli(d, "work", "1")
        assert r.returncode == 0, r.stderr
        return d, td

    def _task_text(self, td):
        return (td / "task.md").read_text(encoding="utf-8")

    def _status(self, td):
        lines = self._task_text(td).splitlines()
        for i, ln in enumerate(lines):
            if ln.strip() == "## Status" and i + 1 < len(lines):
                return lines[i + 1].strip()
        return "?"


class HandoffSection(_Base):
    def test_writes_mechanical_section(self):
        d, td = self._project(code_root=True)
        r = _cli(d, "handoff")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self._task_text(td)
        self.assertIn("## Handoff", text)
        # project git state
        self.assertIn("Project repo", text)
        # a nested code_root line
        self.assertIn("sub", text)
        # gate progress + next unchecked (G2 is the first unchecked)
        self.assertIn("1/3", text)
        self.assertIn("G2: the next thing", text)
        # the latest verification receipt line
        self.assertIn("risk reversible", text)
        # a generated timestamp marker
        self.assertIn("Generated", text)
        # an agent-notes scaffold for the ~20%
        self.assertIn("Agent notes", text)

    def test_prints_agent_instructions(self):
        d, td = self._project()
        r = _cli(d, "handoff")
        out = r.stdout + r.stderr
        self.assertIn("append", out.lower())
        self.assertIn("Handoff", out)

    def test_blocks_with_reason_handoff_no_gate_touched(self):
        d, td = self._project()
        before = self._task_text(td)
        r = _cli(d, "handoff")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._status(td), "blocked")
        text = self._task_text(td)
        self.assertIn("## Blocked", text)
        self.assertIn("handoff", text.split("## Blocked", 1)[1][:120])
        # no gate flipped: same count of checked boxes
        self.assertEqual(before.count("- [x]"), text.count("- [x]"))
        self.assertEqual(before.count("- [ ]"), text.count("- [ ]"))

    def test_no_active_task_errors(self):
        d, td = self._project()
        _cli(d, "handoff")                       # blocks the task
        # deactivate by finishing the block is not needed; use a fresh session id
        r = _cli(d, "handoff", session="pid-none")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("active", (r.stdout + r.stderr).lower())

    def test_degrades_without_git_or_receipt(self):
        d, td = self._project(git=False, receipt=False)
        r = _cli(d, "handoff")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self._task_text(td)
        self.assertIn("## Handoff", text)          # still written
        self.assertEqual(self._status(td), "blocked")


class BootstrapSurfacing(_Base):
    def test_bootstrap_surfaces_unconsumed_handoff(self):
        d, td = self._project()
        _cli(d, "handoff")
        r = _cli(d, "bootstrap")
        out = r.stdout
        self.assertIn("handoff", out.lower())
        self.assertIn("001", out)                  # names the task
        self.assertIn("tasks work 1", out)         # resume hint

    def test_bootstrap_quiet_without_handoff(self):
        # Negative control: a project with an active (non-handoff) task must not
        # print any handoff-resume banner.
        d, td = self._project()
        r = _cli(d, "bootstrap")
        low = r.stdout.lower()
        self.assertNotIn("handoff waiting", low)
        self.assertNotIn("resume with tasks work", low)

    def test_resume_consumes_handoff_section_persists(self):
        d, td = self._project()
        _cli(d, "handoff")
        self.assertEqual(self._status(td), "blocked")
        r = _cli(d, "work", "1")                    # resume
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._status(td), "in_progress")
        # the ## Handoff section stays as history
        self.assertIn("## Handoff", self._task_text(td))
        # and bootstrap no longer surfaces it (consumed)
        r2 = _cli(d, "bootstrap")
        self.assertNotIn("resume with tasks work", r2.stdout.lower())


if __name__ == "__main__":
    unittest.main()
