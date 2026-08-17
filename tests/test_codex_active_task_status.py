#!/usr/bin/env python3
"""Codex parity for F3 (1.5.17): a pointer resolving to a DONE task is not
"active", so the codex apply_patch gate blocks a code edit just like the bash
gate does. Normal close clears the pointer; `tasks work <N>` reopens (status →
in_progress) before editing, so a real resume is unaffected.

Run: python3 -m unittest tests.test_codex_active_task_status
"""
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from provider.codex_hooks import has_active_task, _task_status_is_done  # noqa: E402

SID = "pid-codextest"


class CodexActiveTaskStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.task_dir = self.root / ".agent" / "tasks" / "001-x"
        self.task_dir.mkdir(parents=True)
        self.pointer = self.root / ".agent" / "sessions" / SID / "current_state"
        self.pointer.parent.mkdir(parents=True)
        self.pointer.write_text("001\n", encoding="utf-8")

    def _write_status(self, status: str):
        (self.task_dir / "task.md").write_text(
            f"# 001 - x\n## Status\n{status}\n## Work Plan\n- [ ] g\n", encoding="utf-8")

    def test_in_progress_is_active(self):
        self._write_status("in_progress")
        self.assertTrue(has_active_task(self.root, SID))

    def test_done_is_not_active(self):
        self._write_status("done")
        self.assertFalse(has_active_task(self.root, SID),
                         "a pointer to a DONE task counted as active (F3)")

    def test_missing_status_is_not_done(self):
        # F3 must not turn a parse failure into a new block.
        (self.task_dir / "task.md").write_text(
            "# 001 - x\n(no status)\n- [ ] g\n", encoding="utf-8")
        self.assertTrue(has_active_task(self.root, SID))

    def test_last_status_wins(self):
        (self.task_dir / "task.md").write_text(
            "## Status\nin_progress\n...\n## Status\ndone\n", encoding="utf-8")
        self.assertTrue(_task_status_is_done(self.task_dir / "task.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
