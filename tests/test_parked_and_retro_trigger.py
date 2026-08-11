#!/usr/bin/env python3
"""Point tests for parked lifecycle (P9) + retro trigger (P4).

Pins the fixes for two swallowing failures:
  * 48/68 tasks parked something and nothing ever surfaced it again — so parked
    items must be extractable, classifiable (open/promoted/dismissed), and
    scannable across all tasks, oldest first;
  * retro/intent never fired in 79 tasks because nothing triggered them — so a
    close must be able to propose a retro once enough tasks have closed since the
    last one (and never before, and never double-counting the retro itself).

Pure stdlib unittest. Run: python3 tests/test_parked_and_retro_trigger.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.core import (  # noqa: E402
    PARKED_PLACEHOLDER, count_tasks_since_retro, extract_parked_items,
    open_parked_items, retro_proposal, scan_parked,
)

PLACEHOLDER_SECTION = f"## Parked\n{PARKED_PLACEHOLDER}\n"


def task_md(status="done", parked=None):
    body = f"# 1 - t\n\n## Status\n{status}\n\n## Work Plan\n- [x] g\n"
    if parked is not None:
        body += "\n## Parked\n" + "\n".join(f"- {p}" for p in parked) + "\n"
    return body


class ExtractParked(unittest.TestCase):
    def test_placeholder_yields_nothing(self):
        self.assertEqual(extract_parked_items(PLACEHOLDER_SECTION), [])

    def test_reads_bullets(self):
        self.assertEqual(
            extract_parked_items(task_md(parked=["donut collision", "slow query"])),
            ["donut collision", "slow query"])

    def test_stops_at_next_section(self):
        text = "## Parked\n- keep me\n\n## Debrief\n- not parked\n"
        self.assertEqual(extract_parked_items(text), ["keep me"])

    def test_no_section_is_empty(self):
        self.assertEqual(extract_parked_items("# 1 - t\n\n## Intent\nx\n"), [])


class ParkedStatus(unittest.TestCase):
    def test_open_by_default(self):
        self.assertEqual(open_parked_items(task_md(parked=["raw item"])), ["raw item"])

    def test_promoted_is_not_open(self):
        self.assertEqual(open_parked_items(task_md(parked=["fixed it [promoted → 042]"])), [])

    def test_dismissed_is_not_open(self):
        self.assertEqual(open_parked_items(task_md(parked=["nah [dismissed: wontfix]"])), [])
        self.assertEqual(open_parked_items(task_md(parked=["~~struck out~~"])), [])

    def test_mixed(self):
        items = ["still open", "done [promoted → 9]", "no [dismissed: x]"]
        self.assertEqual(open_parked_items(task_md(parked=items)), ["still open"])


class ScanParked(unittest.TestCase):
    def _proj(self):
        d = Path(tempfile.mkdtemp())
        (d / ".agent" / "tasks").mkdir(parents=True)
        return d

    def _write(self, proj, num, slug, parked):
        td = proj / ".agent" / "tasks" / f"{num:03d}-{slug}"
        td.mkdir(parents=True)
        (td / "task.md").write_text(task_md(parked=parked), encoding="utf-8")

    def test_scans_across_tasks_oldest_first(self):
        p = self._proj()
        self._write(p, 5, "later", ["item-b"])
        self._write(p, 2, "earlier", ["item-a"])
        got = scan_parked(p)
        self.assertEqual([g["task"] for g in got], [2, 5])
        self.assertEqual([g["item"] for g in got], ["item-a", "item-b"])

    def test_open_only_filters_resolved(self):
        p = self._proj()
        self._write(p, 3, "t", ["open one", "closed [promoted → 4]"])
        self.assertEqual([g["item"] for g in scan_parked(p, open_only=True)], ["open one"])
        self.assertEqual(len(scan_parked(p, open_only=False)), 2)


class RetroTrigger(unittest.TestCase):
    def _proj(self):
        d = Path(tempfile.mkdtemp())
        (d / ".agent" / "tasks").mkdir(parents=True)
        return d

    def _task(self, proj, num, slug, status="done"):
        td = proj / ".agent" / "tasks" / f"{num:03d}-{slug}"
        td.mkdir(parents=True)
        (td / "task.md").write_text(task_md(status=status), encoding="utf-8")

    def test_counts_closed_tasks_no_retro(self):
        p = self._proj()
        for i in range(1, 6):
            self._task(p, i, f"task-{i}")
        closed, last = count_tasks_since_retro(p)
        self.assertEqual(closed, 5)
        self.assertIsNone(last)

    def test_only_counts_done(self):
        p = self._proj()
        self._task(p, 1, "a", status="done")
        self._task(p, 2, "b", status="in_progress")
        closed, _ = count_tasks_since_retro(p)
        self.assertEqual(closed, 1)

    def test_resets_after_retro(self):
        p = self._proj()
        for i in range(1, 4):
            self._task(p, i, f"t-{i}")
        self._task(p, 4, "retro-001-003")   # a retro ran
        self._task(p, 5, "after")            # one task since
        closed, last = count_tasks_since_retro(p)
        self.assertEqual(closed, 1)
        self.assertEqual(last, 4)

    def test_proposal_fires_at_threshold(self):
        p = self._proj()
        for i in range(1, 11):
            self._task(p, i, f"t-{i}")
        self.assertIsNotNone(retro_proposal(p, threshold=10))
        self.assertIsNone(retro_proposal(p, threshold=11))

    def test_retro_itself_never_counts(self):
        p = self._proj()
        self._task(p, 1, "retro-000-000")
        closed, last = count_tasks_since_retro(p)
        self.assertEqual(closed, 0)
        self.assertEqual(last, 1)


if __name__ == "__main__":
    unittest.main()
