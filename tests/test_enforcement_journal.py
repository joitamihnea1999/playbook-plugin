#!/usr/bin/env python3
"""Enforcement-event journal — append-only, log-only (branch
feat/enforcement-journal; owner freeze-exception decision 2026-08-21).

A best-effort record of every enforcement DECISION, one JSON object per line, in
the lane-resolved `.agent/<lane>/journal/enforcement.jsonl` (root lane:
`.agent/journal/enforcement.jsonl`). The journal must NEVER change a decision:
the negative control below drives the write to a hard failure (the journal dir
is a regular file, so mkdir and the append both fail) and asserts every
enforcement rc is byte-for-byte what it is without a journal.

Emitters exercised end-to-end as subprocesses (the real enforcement path):
  * task-gate-hook   — a blocked AND an allowed gate decision
  * command_guard.py — a destructive-command block
  * stop-hook        — a stop blocked on open gates
  * gate-batch-check.py — an annotated/bare batch-close block

Stdlib only. Run: python3 tests/test_enforcement_journal.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._bashcheck import bash_or_skip

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "playbook" / "scripts"
SID = "pid-journaltest"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _read_journal(agent_dir: Path) -> "list[dict]":
    p = agent_dir / "journal" / "enforcement.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


class _Base(unittest.TestCase):
    """A legacy-layout (`.agent/tasks/…`) playbook project with one real task."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        self.agent = self.project / ".agent"
        d = self.agent / "tasks" / "001-real"
        d.mkdir(parents=True)
        (d / "task.md").write_text(
            "# 001 - real\n## Status\nin_progress\n"
            "## Work Plan\n- [ ] first gate\n- [ ] second gate\n",
            encoding="utf-8")
        self.session_dir = self.agent / "sessions" / SID
        self.session_dir.mkdir(parents=True)
        self.pointer = self.session_dir / "current_state"

    def _env(self):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = SID
        env.pop("PLAYBOOK_ROLE", None)
        env.pop("PLAYBOOK_EVAL_CONFIG", None)
        return env

    def run_gate(self, file_path, tool="Edit"):
        return subprocess.run(
            [bash_or_skip(), str(SCRIPTS / "task-gate-hook")],
            cwd=self.project, env=self._env(), text=True,
            input=json.dumps({"tool_name": tool,
                              "tool_input": {"file_path": str(file_path)}}),
            capture_output=True)

    def run_stop(self):
        return subprocess.run(
            [bash_or_skip(), str(SCRIPTS / "stop-hook")],
            cwd=self.project, env=self._env(), text=True,
            input=json.dumps({"stop_hook_active": False}),
            capture_output=True)

    def run_command_guard(self, command):
        return subprocess.run(
            ["python3", str(SCRIPTS / "command_guard.py")],
            cwd=self.project, env=self._env(), text=True,
            input=json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command}}),
            capture_output=True)


class TaskGateJournal(_Base):
    def test_block_writes_line(self):
        # No active task → code edit blocked, and the block is journalled.
        r = self.run_gate(self.project / "src" / "main.py")
        self.assertEqual(r.returncode, 2, r.stderr)
        recs = [x for x in _read_journal(self.agent) if x["hook"] == "task-gate"]
        self.assertEqual(len(recs), 1, f"expected one task-gate line: {recs}")
        rec = recs[0]
        self.assertEqual(rec["decision"], "block")
        self.assertEqual(rec["tool"], "Edit")
        self.assertIn("main.py", rec["path"])
        self.assertTrue(rec["reason"])

    def test_allow_writes_line(self):
        # Active task → code edit authorized, and the allow is journalled.
        self.pointer.write_text("001\n", encoding="utf-8")
        r = self.run_gate(self.project / "src" / "main.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        recs = [x for x in _read_journal(self.agent) if x["hook"] == "task-gate"]
        self.assertEqual(len(recs), 1, f"expected one task-gate line: {recs}")
        self.assertEqual(recs[0]["decision"], "allow")
        self.assertIn("main.py", recs[0]["path"])

    def test_exempt_edit_not_journalled(self):
        # .agent/.claude edits are not enforcement decisions → no journal noise.
        self.run_gate(self.agent / "notes.md")
        self.assertEqual(_read_journal(self.agent), [])

    def test_line_format_pinned(self):
        self.pointer.write_text("001\n", encoding="utf-8")
        self.run_gate(self.project / "src" / "main.py")
        rec = _read_journal(self.agent)[0]
        self.assertEqual(list(rec.keys()),
                         ["ts", "session_id", "hook", "decision", "reason", "tool", "path"])
        self.assertRegex(rec["ts"], TS_RE)
        self.assertEqual(rec["session_id"], SID)


class CommandGuardJournal(_Base):
    def test_block_writes_line(self):
        r = self.run_command_guard("rm -rf /")
        self.assertEqual(r.returncode, 2, r.stderr)
        recs = [x for x in _read_journal(self.agent) if x["hook"] == "command-guard"]
        self.assertEqual(len(recs), 1, f"expected one command-guard line: {recs}")
        rec = recs[0]
        self.assertEqual(rec["decision"], "block")
        self.assertEqual(rec["tool"], "Bash")
        self.assertIn("rm -rf /", rec["command"])
        self.assertEqual(list(rec.keys()),
                         ["ts", "session_id", "hook", "decision", "reason", "tool", "command"])

    def test_safe_command_not_journalled(self):
        r = self.run_command_guard("git status")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(_read_journal(self.agent), [])


class StopJournal(_Base):
    def test_block_writes_line(self):
        # Active task, real work done (counters), an open non-Freehand gate → stop blocks.
        self.pointer.write_text("001\n", encoding="utf-8")
        (self.session_dir / "counters").write_text("tools=6\nwrites=2\n", encoding="utf-8")
        r = self.run_stop()
        self.assertEqual(r.returncode, 2, r.stderr)
        recs = [x for x in _read_journal(self.agent) if x["hook"] == "stop"]
        self.assertEqual(len(recs), 1, f"expected one stop line: {recs}")
        self.assertEqual(recs[0]["decision"], "block")
        self.assertEqual(list(recs[0].keys()),
                         ["ts", "session_id", "hook", "decision", "reason"])


class BatchCloseJournal(_Base):
    def run_batch(self, old, new):
        task_md = self.agent / "tasks" / "001-real" / "task.md"
        return subprocess.run(
            ["python3", str(SCRIPTS / "gate-batch-check.py"),
             "--tool", "Edit", "--file", str(task_md),
             "--session-dir", str(self.session_dir)],
            cwd=self.project, env=self._env(), text=True,
            input=json.dumps({"tool_input": {"old_string": old, "new_string": new}}),
            capture_output=True)

    def test_bare_batch_block_writes_line(self):
        old = "- [ ] alpha\n- [ ] bravo\n"
        new = "- [x] alpha\n- [x] bravo\n"   # 2 closes, no annotation → block
        r = self.run_batch(old, new)
        self.assertEqual(r.returncode, 2, r.stdout)
        recs = [x for x in _read_journal(self.agent) if x["hook"] == "batch-close"]
        self.assertEqual(len(recs), 1, f"expected one batch-close line: {recs}")
        self.assertEqual(recs[0]["decision"], "block")
        self.assertEqual(list(recs[0].keys()),
                         ["ts", "session_id", "hook", "decision", "reason", "tool", "path"])


class MultiUserLaneResolution(unittest.TestCase):
    """The journal lands in the resolved per-user lane, not the shared root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        self.agent = self.project / ".agent"
        (self.agent).mkdir(parents=True)
        (self.agent / "current_user").write_text("alice\n", encoding="utf-8")
        self.lane = self.agent / "alice"
        d = self.lane / "tasks" / "001-real"
        d.mkdir(parents=True)
        (d / "task.md").write_text(
            "# 001\n## Status\nin_progress\n## Work Plan\n- [ ] g\n", encoding="utf-8")

    def _env(self):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = SID
        return env

    def test_block_written_into_lane_not_root(self):
        r = subprocess.run(
            [bash_or_skip(), str(SCRIPTS / "task-gate-hook")],
            cwd=self.project, env=self._env(), text=True,
            input=json.dumps({"tool_name": "Edit",
                              "tool_input": {"file_path": str(self.project / "src" / "x.py")}}),
            capture_output=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        # Written into alice's lane…
        self.assertTrue((self.lane / "journal" / "enforcement.jsonl").exists(),
                        "journal not written into resolved lane .agent/alice/")
        # …and NOT into the shared root.
        self.assertFalse((self.agent / "journal" / "enforcement.jsonl").exists(),
                         "journal leaked into shared root .agent/")


class FreshCloneNoRootJournal(unittest.TestCase):
    """Fresh clone of a multi-user repo: per-user lanes exist but the gitignored
    `.agent/current_user` marker does not. The gate still fail-closed BLOCKS, but
    the journal must NOT be written to the shared root `.agent/` — that would mint
    phantom root-lane state (the S15 invariant)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        self.agent = self.project / ".agent"
        d = self.agent / "alice" / "tasks" / "001-real"   # a lane, but no marker
        d.mkdir(parents=True)
        (d / "task.md").write_text(
            "# 001\n## Status\nin_progress\n## Work Plan\n- [ ] g\n", encoding="utf-8")

    def test_block_writes_no_root_journal(self):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = SID
        r = subprocess.run(
            [bash_or_skip(), str(SCRIPTS / "task-gate-hook")],
            cwd=self.project, env=env, text=True,
            input=json.dumps({"tool_name": "Edit",
                              "tool_input": {"file_path": str(self.project / "src" / "x.py")}}),
            capture_output=True)
        self.assertEqual(r.returncode, 2, r.stderr)   # fail-closed still blocks
        self.assertFalse((self.agent / "journal").exists(),
                         "journal minted phantom root-lane state on a fresh clone")


class NegativeControl(_Base):
    """The journal must NEVER change a decision. Drive the write to a hard
    failure (journal path is a regular FILE, so mkdir and append both fail) and
    assert every enforcement rc is exactly what it is without a journal."""

    def _wedge_journal(self, agent_dir: Path):
        # `.agent/<lane>/journal` as a regular file: mkdir(journal) and the
        # append both fail. If any emitter let that error escape, an rc would
        # change — that is precisely what this control forbids.
        (agent_dir / "journal").write_text("not a directory\n", encoding="utf-8")

    def test_block_still_blocks(self):
        self._wedge_journal(self.agent)
        r = self.run_gate(self.project / "src" / "main.py")
        self.assertEqual(r.returncode, 2, "journal failure changed a BLOCK decision")
        self.assertIn("BLOCKED", r.stderr)

    def test_allow_still_allows(self):
        self._wedge_journal(self.agent)
        self.pointer.write_text("001\n", encoding="utf-8")
        r = self.run_gate(self.project / "src" / "main.py")
        self.assertEqual(r.returncode, 0, "journal failure changed an ALLOW decision")

    def test_command_guard_still_blocks(self):
        self._wedge_journal(self.agent)
        r = self.run_command_guard("rm -rf /")
        self.assertEqual(r.returncode, 2, "journal failure changed a command-guard BLOCK")

    def test_stop_still_blocks(self):
        self._wedge_journal(self.agent)
        self.pointer.write_text("001\n", encoding="utf-8")
        (self.session_dir / "counters").write_text("tools=6\nwrites=2\n", encoding="utf-8")
        r = self.run_stop()
        self.assertEqual(r.returncode, 2, "journal failure changed a stop BLOCK")


class NeverCreatesAgentDir(unittest.TestCase):
    """A non-playbook project (no .agent) must stay non-playbook: no journal, and
    crucially no .agent minted as a side effect."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "bare"
        self.project.mkdir(parents=True)

    def test_command_guard_no_agent_dir(self):
        subprocess.run(
            ["python3", str(SCRIPTS / "command_guard.py")],
            cwd=self.project, text=True,
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}),
            capture_output=True)
        self.assertFalse((self.project / ".agent").exists(),
                         ".agent was created as a side effect of journalling")


if __name__ == "__main__":
    unittest.main(verbosity=2)
