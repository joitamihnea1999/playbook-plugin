"""`tasks work done` must resolve the session pointer to a REAL task before
doing anything destructive (verification-report-1.5.9 C1 + C1b).

C1: `lifecycle.cmd_work` read `task_file` (assigned only inside `if matches:`)
unconditionally after the block, and ran the session-pointer wipe + printed
"Task X done." OUTSIDE the `if matches:` guard. So when `current_state` names a
task whose `NNN-*` glob matches nothing (renamed/deleted folder, wrong-lane
pointer, or the C1b substring bug writing a raw non-padded pointer), the close
path printed a FALSE "done", deleted every session dir pointing at it, never
wrote `## Status`, then crashed with `UnboundLocalError: task_file`. This is the
single most dangerous defect for autonomous use — the agent is told finished
work is complete when it is not, and loses its pointer.

C1b: `_find_active_task` matched the name filter as a SUBSTRING
(`name_filter not in folder`), so `tasks work 100` activated `1000-bar` — and
wrote the raw pointer `100`, feeding the C1 non-resolving-pointer crash.

Everything runs the real CLI as a subprocess: the bug lived in argv dispatch and
process-level state (pointer file, exit code, stderr).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"

SESSION_ID = "pid-c1-test"

TASK_MD = """# {num} - Fixture

## Status
{status}

## Risk
reversible

## Intent
Fixture task.

## Work Plan
- [{gate}] only gate
"""


class WorkDonePointerBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def run_tasks(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["PLAYBOOK_SESSION_ID"] = SESSION_ID
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", *args],
            cwd=self.project, env=env, capture_output=True, text=True,
        )

    def write_task(self, num: str, slug: str, status: str = "pending",
                   gate_checked: bool = True) -> Path:
        d = self.project / ".agent" / "tasks" / f"{num}-{slug}"
        d.mkdir(parents=True, exist_ok=True)
        tf = d / "task.md"
        tf.write_text(
            TASK_MD.format(num=num, status=status,
                           gate="x" if gate_checked else " "),
            encoding="utf-8")
        return tf

    def write_pointer(self, value: str) -> Path:
        p = self.project / ".agent" / "sessions" / SESSION_ID / "current_state"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{value}\n", encoding="utf-8")
        return p

    @property
    def pointer(self) -> Path:
        return self.project / ".agent" / "sessions" / SESSION_ID / "current_state"

    def status_of(self, tf: Path) -> str:
        lines = tf.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "## Status" and i + 1 < len(lines):
                return lines[i + 1].strip()
        return ""


class TestNonResolvingPointer(WorkDonePointerBase):
    """C1: `work done` on a pointer that does not glob-resolve must fail loud
    and change NOTHING."""

    def test_nonresolving_pointer_does_not_print_false_done(self):
        # A pointer naming a task whose NNN-* folder does not exist.
        self.write_task("001", "my-fix")  # real task, but pointer names "my"
        self.write_pointer("my")
        r = self.run_tasks("work", "done")
        # No false "done" reported.
        self.assertNotIn("done.", r.stdout,
                         f"printed a false done: {r.stdout!r}")
        # Non-zero exit (fail loud).
        self.assertNotEqual(r.returncode, 0,
                            f"non-resolving pointer succeeded silently: {r.stdout!r}")
        # No crash / traceback leaking to the user.
        self.assertNotIn("UnboundLocalError", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_nonresolving_pointer_does_not_wipe_sessions(self):
        self.write_task("001", "my-fix")
        self.write_pointer("my")
        self.run_tasks("work", "done")
        # The session dir (and pointer) must survive — nothing destructive ran.
        self.assertTrue(self.pointer.exists(),
                        "the session pointer was wiped on a non-resolving close")

    def test_nonresolving_pointer_never_writes_status(self):
        tf = self.write_task("001", "my-fix")
        self.write_pointer("my")
        self.run_tasks("work", "done")
        self.assertEqual(self.status_of(tf), "pending",
                         "Status was mutated for an unresolved pointer")


class TestSubstringActivation(WorkDonePointerBase):
    """C1b: `_find_active_task` used a substring match, so `work 100` could
    activate `1000-bar` and write the raw pointer `100`."""

    def test_work_100_does_not_activate_1000(self):
        # Open gate so `_find_active_task` would RETURN the substring match.
        self.write_task("1000", "bar", gate_checked=False)
        r = self.run_tasks("work", "100")
        # 100 does not exist; it must NOT resolve to 1000-bar.
        self.assertNotEqual(r.returncode, 0,
                            f"work 100 wrongly succeeded against 1000-bar: {r.stdout!r}")
        # And it must not have written a pointer to 1000 (or a raw 100 pointer).
        if self.pointer.exists():
            self.assertNotEqual(self.pointer.read_text(encoding="utf-8").strip(), "1000")

    def test_exact_number_still_activates(self):
        """Negative control: the real task at the exact number still works."""
        self.write_task("100", "bar")
        r = self.run_tasks("work", "100")
        self.assertEqual(r.returncode, 0, f"exact match refused: {r.stderr}")
        self.assertEqual(self.pointer.read_text(encoding="utf-8").strip(), "100")


class TestResolvingPointerStillCloses(WorkDonePointerBase):
    """Negative control: a resolving pointer closes normally."""

    def test_good_pointer_closes(self):
        tf = self.write_task("001", "real")
        self.run_tasks("work", "001")
        r = self.run_tasks("work", "done")
        self.assertEqual(r.returncode, 0, f"good close refused: {r.stderr}")
        self.assertEqual(self.status_of(tf), "done")


if __name__ == "__main__":
    unittest.main()
