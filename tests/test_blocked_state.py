#!/usr/bin/env python3
"""A first-class BLOCKED state (upstream issue #08).

A task can reach a checkpoint whose next gate is the owner's decision, not the
agent's work. With no honest state for that, the agent's options were all lies:
check the gate anyway, `work done` (false), or misuse `freehand`. This adds a
`blocked` state so the pause is self-documenting.

Invariants (from the write-up):
  * the Stop hook exits 0 on a blocked task with unchecked gates, FIRST attempt
    (stop_hook_active:false — the retry valve would mask a broken implementation);
  * blocking checks/adds/reorders NO gate — gate lines byte-identical before/after;
  * `tasks list`/`status` show BLOCKED, counted separately from pending and done;
  * `_find_active_task` skips a blocked task;
  * `tasks work <N>` clears it and the Stop hook blocks again afterwards;
  * the reason cannot break the gate parsers — a reason containing `- [ ]`, a `## `
    heading, or backticks must not become a phantom gate/section (see #09).

Run: python3 tests/test_blocked_state.py
"""
import os
import subprocess
from tests._bashcheck import bash_or_skip
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(PLUGIN))
from tasks.core import (  # noqa: E402
    _extract_status, _find_active_task, _gate_counts, _is_blocked,
    resume_blocked_task, set_task_blocked,
)

SID = "pid-blocked-test"

TASK = """# {n} - Decide

## Status
pending

## Work Plan
- [x] G1: measure current p99
- [ ] G2: if p99 > 200ms rewrite the index; if under, cancel
"""


class SetBlockedPure(unittest.TestCase):
    def _task(self, body=TASK.format(n="012")):
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text(body, encoding="utf-8")
        return tf

    def test_sets_status_and_records_reason(self):
        tf = self._task()
        set_task_blocked(tf, "waiting on owner: rewrite or cancel?")
        self.assertTrue(_is_blocked(tf))
        text = tf.read_text(encoding="utf-8")
        self.assertIn("## Blocked", text)
        self.assertIn("waiting on owner", text)

    def test_blocking_does_not_touch_gates(self):
        tf = self._task()
        before = _gate_counts(tf.read_text(encoding="utf-8"))
        gate_lines_before = [l for l in tf.read_text().splitlines() if l.lstrip().startswith("- [")]
        set_task_blocked(tf, "pause")
        after = _gate_counts(tf.read_text(encoding="utf-8"))
        gate_lines_after = [l for l in tf.read_text().splitlines() if l.lstrip().startswith("- [")]
        self.assertEqual(before, after)
        self.assertEqual(gate_lines_before, gate_lines_after)

    def test_hostile_reason_cannot_forge_a_gate_or_heading(self):
        tf = self._task()
        before = _gate_counts(tf.read_text(encoding="utf-8"))
        set_task_blocked(tf, "- [ ] fake gate\n## Fake Heading\n`weird`")
        after = _gate_counts(tf.read_text(encoding="utf-8"))
        self.assertEqual(before, after, "a reason must never mint a phantom gate (#09)")
        # The reason still round-trips as readable text.
        self.assertIn("fake gate", tf.read_text(encoding="utf-8"))

    def test_reblock_is_idempotent(self):
        tf = self._task()
        set_task_blocked(tf, "first reason")
        set_task_blocked(tf, "second reason")
        text = tf.read_text(encoding="utf-8")
        self.assertEqual(text.count("## Blocked"), 1)
        self.assertIn("second reason", text)
        self.assertNotIn("first reason", text)

    def test_resume_flips_status_and_stamps(self):
        tf = self._task()
        set_task_blocked(tf, "pause")
        resume_blocked_task(tf)
        self.assertEqual(_extract_status(tf), "in_progress")
        self.assertIn("Resumed", tf.read_text(encoding="utf-8"))

    def test_find_active_task_skips_blocked(self):
        proj = Path(tempfile.mkdtemp())
        td = proj / ".agent" / "tasks" / "012-decide"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text(TASK.format(n="012"), encoding="utf-8")
        self.assertIsNotNone(_find_active_task(proj))   # active while pending
        set_task_blocked(tf, "pause")
        self.assertIsNone(_find_active_task(proj), "a blocked task must not be active")


class BlockedEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        td = self.project / ".agent" / "tasks" / "012-decide"
        td.mkdir(parents=True)
        (td / "task.md").write_text(TASK.format(n="012"), encoding="utf-8")
        self.task_file = td / "task.md"

    def _env(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["PLAYBOOK_SESSION_ID"] = SID
        return env

    def run_tasks(self, *args):
        return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                              cwd=self.project, env=self._env(), capture_output=True, text=True)

    def run_stop_hook(self, stop_active=False):
        payload = '{"stop_hook_active": %s}' % ("true" if stop_active else "false")
        return subprocess.run([bash_or_skip(), str(SCRIPTS / "stop-hook")],
                              input=payload, cwd=self.project, env=self._env(),
                              capture_output=True, text=True)

    def _set_counters(self):
        # High activity so the conversational bypass (writes==0 & tools<5) does NOT
        # fire — otherwise a green exit would not prove the BLOCKED path ran.
        sd = self.project / ".agent" / "sessions" / SID
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "counters").write_text("writes=9\ntools=40\n", encoding="utf-8")

    def test_full_lifecycle(self):
        # activate
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self._set_counters()

        # unchecked gate + real activity → hook blocks
        self.assertEqual(self.run_stop_hook().returncode, 2,
                         "open gates with activity must block")

        # block it
        b = self.run_tasks("blocked", "p99 measured; rewrite or cancel is your call")
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertEqual(_extract_status(self.task_file), "blocked")

        # THE invariant: hook allows the turn to end, on the FIRST attempt
        self._set_counters()
        self.assertEqual(self.run_stop_hook(stop_active=False).returncode, 0,
                         "a blocked task must let the turn end without a fake checkbox")

        # list + status show BLOCKED, counted separately
        lst = self.run_tasks("list")
        self.assertIn("blocked", lst.stdout)
        self.assertIn("1 blocked", lst.stdout)
        st = self.run_tasks("status")
        self.assertIn("BLOCKED", st.stdout)

        # resume clears it, and the hook blocks again
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        self.assertEqual(_extract_status(self.task_file), "in_progress")
        self._set_counters()
        self.assertEqual(self.run_stop_hook().returncode, 2,
                         "after resume, open gates block again")

    def test_blocked_requires_a_reason(self):
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)
        r = self.run_tasks("blocked")
        self.assertEqual(r.returncode, 1)
        self.assertIn("reason", r.stderr.lower())

    def test_duplicate_status_headings_hook_agrees_with_python(self):
        """Parity: core._extract_status reads the line after the LAST `## Status`;
        the stop-hook's awk must apply the same rule, or a stray duplicate heading
        makes Python and the enforcing hook disagree about the same file (the #09
        disease in a new spot)."""
        self.assertEqual(self.run_tasks("work", "012").returncode, 0)

        # Decoy heading FIRST says pending, real LAST says blocked →
        # Python reads blocked, so the hook must allow the turn to end (exit 0).
        self.task_file.write_text(
            "# 012 - Decide\n\n## Status\npending\n\n## Work Plan\n"
            "- [ ] open gate\n\n## Status\nblocked\n\n## Blocked\n> waiting\n",
            encoding="utf-8")
        self.assertEqual(_extract_status(self.task_file), "blocked")
        self._set_counters()
        self.assertEqual(self.run_stop_hook().returncode, 0,
                         "hook must honor the LAST ## Status, as Python does")

        # Reverse: decoy FIRST says blocked, real LAST says pending →
        # Python reads pending, so the hook must still block on the open gate.
        self.task_file.write_text(
            "# 012 - Decide\n\n## Status\nblocked\n\n## Work Plan\n"
            "- [ ] open gate\n\n## Status\npending\n",
            encoding="utf-8")
        self.assertEqual(_extract_status(self.task_file), "pending")
        self._set_counters()
        self.assertEqual(self.run_stop_hook().returncode, 2,
                         "a decoy 'blocked' heading must not release the gate")


if __name__ == "__main__":
    unittest.main()
