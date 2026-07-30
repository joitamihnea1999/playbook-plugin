"""`tasks work <N>` must be able to re-adopt a finished-but-unclosed task (task 027).

The field report that opened task 027 ended in a state the CLI could not express:
all 41 gates checked, `## Status` still `pending`, and no session pointer (the
SessionStart GC had deleted it). Both documented routes were closed —

  * `tasks work done` reads the pointer; absent → "No active task." and it
    never touches `## Status`;
  * `tasks work 56` was REFUSED, because `_find_active_task` only returns tasks
    that still have unchecked gates and the fallback only re-activated a task
    whose status was already `done` (reopen) or a stub.

The only sanctioned writer of `## Status` needed a pointer, and the only way to
get a pointer was refused, so the pointer had to be hand-written. This module
pins the third fallback arm that closes that hole, plus the branches it must not
disturb.

Everything runs the real CLI as a subprocess: the bug lived in argv dispatch and
process-level state (pointer file, exit code, stderr), which an in-process call
would not reproduce.
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

SESSION_ID = "pid-readopt-test"

# A task.md with the two structural features the arms key on: a `## Status`
# block and gate checkboxes. Gate text is irrelevant; only checked-ness is.
TASK_MD = """# {num} - Readopt Fixture

## Status
{status}

## Intent
Fixture task for the re-adoption arm.

## Work Plan
- [{g1}] first gate
- [{g2}] second gate
"""


def write_task(project: Path, num: str, status: str, gates_checked: bool,
               stub: bool = False) -> Path:
    """Create .agent/tasks/<num>-readopt/task.md and return its path."""
    d = project / ".agent" / "tasks" / f"{num}-readopt"
    d.mkdir(parents=True, exist_ok=True)
    mark = "x" if gates_checked else " "
    body = TASK_MD.format(num=num, status=status, g1=mark, g2=mark)
    if stub:
        body = body.replace("## Intent", "<!-- stub:bugfix -->\n\n## Intent")
    tf = d / "task.md"
    tf.write_text(body, encoding="utf-8")
    return tf


def status_of(task_file: Path) -> str:
    lines = task_file.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Status" and i + 1 < len(lines):
            return lines[i + 1].strip()
    return ""


class WorkReadoptBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def run_tasks(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        # Pin the session id so the pointer lands somewhere we can assert on,
        # and so _gc_dead_sessions self-excludes it (a non-numeric name that
        # `kill -0` would reject).
        env["PLAYBOOK_SESSION_ID"] = SESSION_ID
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", *args],
            cwd=self.project, env=env, capture_output=True, text=True,
        )

    @property
    def pointer(self) -> Path:
        return self.project / ".agent" / "sessions" / SESSION_ID / "current_state"


class TestReadoptFullyGated(WorkReadoptBase):
    """The state the field report got stuck in."""

    def test_pending_fully_gated_task_is_readopted(self):
        tf = write_task(self.project, "056", "pending", gates_checked=True)
        r = self.run_tasks("work", "056")
        self.assertEqual(r.returncode, 0, f"work was refused: {r.stderr or r.stdout}")
        self.assertNotIn("has no open gates", r.stderr)
        self.assertIn("re-adopting", r.stdout)
        self.assertTrue(self.pointer.exists(), "no session pointer was written")
        self.assertEqual(self.pointer.read_text(encoding="utf-8").strip(), "056")
        # Re-adoption must NOT close the task behind the user's back — `work
        # done` stays the only writer of ## Status.
        self.assertEqual(status_of(tf), "pending")

    def test_in_progress_fully_gated_task_is_readopted(self):
        """`_is_done` only special-cases `done`, and `_find_active_task` skips
        every fully-gated task regardless of status — so `in_progress` reaches
        the same dead end as `pending` and must recover the same way."""
        tf = write_task(self.project, "057", "in_progress", gates_checked=True)
        r = self.run_tasks("work", "057")
        self.assertEqual(r.returncode, 0, f"work was refused: {r.stderr or r.stdout}")
        self.assertEqual(self.pointer.read_text(encoding="utf-8").strip(), "057")
        self.assertEqual(status_of(tf), "in_progress")

    def test_readopt_then_work_done_closes_the_task(self):
        """The whole point: the recovery has to reach a closed task through the
        CLI, with the CLI writing ## Status."""
        tf = write_task(self.project, "058", "pending", gates_checked=True)
        self.assertEqual(self.run_tasks("work", "058").returncode, 0)
        r = self.run_tasks("work", "done")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(status_of(tf), "done")

    def test_readopt_is_idempotent_on_the_already_pointed_task(self):
        """Re-running `work <N>` on the task already in the pointer takes the
        `prev_task == task_num` path, which skips the auto-close block. It must
        not corrupt status or fail."""
        tf = write_task(self.project, "059", "pending", gates_checked=True)
        first = self.run_tasks("work", "059")
        second = self.run_tasks("work", "059")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.pointer.read_text(encoding="utf-8").strip(), "059")
        self.assertEqual(status_of(tf), "pending")

    def test_pointer_loss_mid_task_is_recoverable_end_to_end(self):
        """The field scenario in full: activate, lose the pointer the way the GC
        used to lose it, then recover through the CLI alone."""
        tf = write_task(self.project, "060", "pending", gates_checked=False)
        self.assertEqual(self.run_tasks("work", "060").returncode, 0)
        # All gates get checked during the work...
        tf.write_text(tf.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                      encoding="utf-8")
        # ...and then the sweep deletes the session dir under a live session.
        import shutil
        shutil.rmtree(self.pointer.parent)
        self.assertFalse(self.pointer.exists())

        readopt = self.run_tasks("work", "060")
        self.assertEqual(readopt.returncode, 0,
                         f"the dead end is back: {readopt.stderr or readopt.stdout}")
        done = self.run_tasks("work", "done")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(status_of(tf), "done")


class TestUntouchedBranches(WorkReadoptBase):
    """The new arm is last in the chain; nothing above or below may shift."""

    def test_done_task_still_reopens(self):
        tf = write_task(self.project, "061", "done", gates_checked=True)
        r = self.run_tasks("work", "061")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reopening", r.stdout)
        self.assertEqual(status_of(tf), "in_progress",
                         "the reopen branch must still rewrite ## Status")

    def test_stub_still_activates(self):
        write_task(self.project, "062", "pending", gates_checked=True, stub=True)
        r = self.run_tasks("work", "062")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("re-adopting", r.stdout,
                         "a stub must take the stub arm, not the re-adoption arm")

    def test_task_with_open_gates_activates_normally(self):
        tf = write_task(self.project, "063", "pending", gates_checked=False)
        r = self.run_tasks("work", "063")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("re-adopting", r.stdout)
        self.assertEqual(status_of(tf), "pending")

    def test_missing_task_still_fails(self):
        r = self.run_tasks("work", "099")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr)

    def test_work_done_without_a_pointer_still_reports_no_active_task(self):
        """Fix B deliberately does not change this: `work done` still refuses to
        guess which task was active."""
        write_task(self.project, "064", "pending", gates_checked=True)
        r = self.run_tasks("work", "done")
        self.assertEqual(r.returncode, 0)
        self.assertIn("No active task", r.stdout)

    def test_gate_bounce_still_blocks_closing_a_task_with_open_gates(self):
        tf = write_task(self.project, "065", "pending", gates_checked=False)
        self.assertEqual(self.run_tasks("work", "065").returncode, 0)
        r = self.run_tasks("work", "done")
        self.assertEqual(r.returncode, 1, "open gates must still bounce")
        self.assertNotEqual(status_of(tf), "done")


class TestAdjacentBehaviour(WorkReadoptBase):
    """Side effects the re-adoption arm must not disturb."""

    def test_switching_away_still_auto_closes_a_fully_gated_previous_task(self):
        """The auto-close branch fires on `prev_task != task_num`, i.e. a
        DIFFERENT target. The new arm keys on the target task, so it must not
        shadow it: switching from a finished-but-open task still closes it."""
        prev = write_task(self.project, "070", "pending", gates_checked=True)
        nxt = write_task(self.project, "071", "pending", gates_checked=False)
        self.assertEqual(self.run_tasks("work", "070").returncode, 0)
        r = self.run_tasks("work", "071")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Auto-closed task 070", r.stdout)
        self.assertEqual(status_of(prev), "done")
        self.assertEqual(status_of(nxt), "pending")

    def test_status_reports_a_readopted_task(self):
        """`tasks status` reads the pointer, so a re-adopted task must show up
        there — that is how the agent sees it is active again."""
        write_task(self.project, "072", "pending", gates_checked=True)
        self.assertEqual(self.run_tasks("work", "072").returncode, 0)
        r = self.run_tasks("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("072", r.stdout)


class TestForceInteraction(unittest.TestCase):
    """`--force` shares `cmd work` with the re-adoption arm.

    Verify B1 listed force as a regression arm and the first suite did not cover
    it (impl panel caught the gap), so a force-interaction bug would have stayed
    green.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    run_tasks = WorkReadoptBase.run_tasks
    pointer = WorkReadoptBase.pointer

    def test_force_switch_from_an_open_task_onto_a_readopt_target(self):
        """Without --force this bounces; with it, the switch lands on the
        fully-gated target through the new arm and leaves the abandoned task
        in_progress rather than silently closing it."""
        open_task = write_task(self.project, "080", "pending", gates_checked=False)
        target = write_task(self.project, "081", "pending", gates_checked=True)
        self.assertEqual(self.run_tasks("work", "080").returncode, 0)

        bounced = self.run_tasks("work", "081")
        self.assertEqual(bounced.returncode, 1, "open gates must still bounce without --force")

        forced = self.run_tasks("work", "081", "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIn("re-adopting", forced.stdout)
        self.assertEqual(self.pointer.read_text(encoding="utf-8").strip(), "081")
        self.assertNotEqual(status_of(open_task), "done",
                            "--force must not silently close the abandoned task")
        self.assertEqual(status_of(target), "pending")

    def test_force_work_done_closes_a_task_with_open_gates(self):
        tf = write_task(self.project, "082", "pending", gates_checked=False)
        self.assertEqual(self.run_tasks("work", "082").returncode, 0)
        r = self.run_tasks("work", "done", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(status_of(tf), "done")


if __name__ == "__main__":
    unittest.main()
