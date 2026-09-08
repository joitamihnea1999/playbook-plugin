#!/usr/bin/env python3
"""Gate-logging gap detector (task 018 / bug report #4).

Pure stdlib unittest. Run: python3 tests/test_gate_logging.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(_PLUGIN))

from tasks.gate_logging import done_task_numbers, gate_logging_gap  # noqa: E402


def _log(m_count=3, g_tasks=()):
    """Synthesize a chat_log with `m_count` message entries and G-entries for
    the given task numbers."""
    parts = []
    for i in range(1, m_count + 1):
        parts.append(f"**[M{i:03d}]** [2026-07-01 10:00:00 UTC] `HOST` (claude/pid-x)\n\nhi\n\n---\n")
    for t in g_tasks:
        parts.append(f"**[G{t:03d}:12]** [2026-07-01 10:05:00 UTC] `HOST` (3 tool calls)\n\n- [x] a gate\n\n---\n")
    return "\n".join(parts)


class GateLoggingGapTest(unittest.TestCase):
    def test_gap_fires_when_logging_stopped(self):
        # Gate entries stop at 064; done tasks run to 110 → warn (the cristi case).
        text = _log(m_count=5, g_tasks=[60, 62, 64])
        done = list(range(1, 111))
        msg = gate_logging_gap(text, done)
        self.assertIsNotNone(msg)
        self.assertIn("064", msg)
        self.assertIn("110", msg)

    def test_healthy_lane_no_warning(self):
        # Gate logging keeps pace with done tasks → no warning (the marius case).
        text = _log(m_count=5, g_tasks=[48, 49, 50])
        done = list(range(1, 51))
        self.assertIsNone(gate_logging_gap(text, done))

    def test_one_newer_task_below_min_gap(self):
        # Only a single newer done task → below min_gap, don't cry wolf.
        text = _log(m_count=3, g_tasks=[10])
        self.assertIsNone(gate_logging_gap(text, [8, 9, 10, 11]))

    def test_no_gate_entries_at_all_but_log_alive(self):
        text = _log(m_count=4, g_tasks=[])
        msg = gate_logging_gap(text, [1, 2, 3])
        self.assertIsNotNone(msg)
        self.assertIn("never have fired", msg)

    def test_no_signal_when_log_not_alive(self):
        # No M-entries → no evidence the lane is active → stay silent.
        self.assertIsNone(gate_logging_gap("", [1, 2, 3, 4, 5]))
        self.assertIsNone(gate_logging_gap("random text no entries", [1, 2, 3]))

    def test_no_signal_without_done_tasks(self):
        text = _log(m_count=3, g_tasks=[5])
        self.assertIsNone(gate_logging_gap(text, []))


class DoneTaskNumbersTest(unittest.TestCase):
    def _mk(self, tasks_dir, name, status):
        d = tasks_dir / name
        d.mkdir(parents=True)
        (d / "task.md").write_text(f"# {name}\n\n## Status\n{status}\n", encoding="utf-8")

    def test_fenced_status_decoy_is_not_done(self):
        # Task 043 round-3 panel (opus): this reader used its own fence-blind regex;
        # it must use the shared fence-aware status reader like every other consumer.
        root = Path(tempfile.mkdtemp()) / ".agent" / "tasks"
        d = root / "004-decoy"; d.mkdir(parents=True)
        # The decoy sits FIRST: the old first-match regex reader read `done` here.
        (d / "task.md").write_text(
            "# 004\n\n## Docs\n```\n## Status\ndone\n```\n\n## Status\npending\n",
            encoding="utf-8")
        self._mk(root, "005-real", "done")
        self.assertEqual(done_task_numbers(root), [5])

    def test_counts_only_done(self):
        root = Path(tempfile.mkdtemp()) / "tasks"
        root.mkdir()
        self._mk(root, "001-a", "done")
        self._mk(root, "002-b", "pending")
        self._mk(root, "003-c", "done")
        self.assertEqual(done_task_numbers(root), [1, 3])

    def test_missing_dir_empty(self):
        self.assertEqual(done_task_numbers(Path(tempfile.mkdtemp()) / "nope"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
