#!/usr/bin/env python3
"""`tasks parked` must see a promotion whose target task no longer exists (PLAN S4).

Task 071 deleted the stub task dirs 061-069; 25 `[promoted → 06N]` markers kept
pointing at them, and the classifier called every `[promoted…]` resolved — so the
debt was invisible and "zero open parked" was satisfiable with 25 findings
unlabelled. A numeric promotion to a MISSING task dir is now `dangling` and counts
as open; a dated `[deferred: …]` records an owner decision and is not open, but is
counted so it cannot vanish.

Run: python3 -m unittest tests.test_parked_dangling
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins" / "playbook"
sys.path.insert(0, str(PLUGIN))

from tasks.core import scan_parked  # noqa: E402

TASK = "# {n} - T\n\n## Status\ndone\n\n## Parked\n{bullets}\n"


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tasks = self.root / ".agent" / "tasks"
        self.tasks.mkdir(parents=True)

    def _task(self, num: int, bullets: "list[str]"):
        d = self.tasks / f"{num:03d}-t"
        d.mkdir()
        (d / "task.md").write_text(TASK.format(n=num, bullets="\n".join(f"- {b}" for b in bullets)),
                                   encoding="utf-8")

    def _items(self, open_only=True):
        return {it["item"]: it["status"] for it in scan_parked(self.root, open_only=open_only)}


class DanglingPromotion(_Fixture):
    def test_missing_target_dir_is_listed_open_with_dangling_tag(self):
        self._task(1, ["lost finding [promoted → 062]"])            # 062 does not exist
        items = self._items()
        self.assertEqual(items, {"lost finding [promoted → 062]": "dangling"})

    def test_existing_target_dir_stays_resolved(self):
        self._task(1, ["kept finding [promoted → 002]"])
        self._task(2, [])
        self.assertEqual(self._items(), {})
        self.assertEqual(self._items(open_only=False), {"kept finding [promoted → 002]": "promoted"})

    def test_one_missing_target_among_several_is_dangling(self):
        self._task(1, ["split finding [promoted → 002] [promoted → 009]"])
        self._task(2, [])
        self.assertEqual(self._items(), {"split finding [promoted → 002] [promoted → 009]": "dangling"})

    def test_a_dismissal_on_the_same_line_resolves_a_dangling_marker(self):
        self._task(1, ["old stub [promoted → 063] [dismissed: 071 2026-09-09 — hygiene, no defect]"])
        self.assertEqual(self._items(), {})

    def test_a_promotion_to_a_named_plan_step_is_resolved(self):
        self._task(1, ["handed on [promoted → PLAN S7]"])
        self.assertEqual(self._items(open_only=False), {"handed on [promoted → PLAN S7]": "promoted"})


class OneGrammarAndQuotations(_Fixture):
    """Impl panel round 1 (opus, sol-high, sol-medium, grok)."""

    def test_the_legacy_arrow_task_form_is_checked_too(self):
        self._task(1, ["moved on → task 062"])                      # 062 missing
        self.assertEqual(set(self._items().values()), {"dangling"})

    def test_a_marker_quoted_in_inline_code_is_not_a_disposition(self):
        self._task(1, ["the raw `[promoted → 06N]` count differs from the dangling one"])
        self.assertEqual(set(self._items().values()), {"open"})

    def test_the_close_and_activation_helper_counts_dangling(self):
        from tasks.core import existing_task_numbers, open_parked_items
        self._task(1, ["lost [promoted → 062]", "kept [promoted → 001]"])
        text = (self.tasks / "001-t" / "task.md").read_text(encoding="utf-8")
        self.assertEqual(open_parked_items(text, existing_task_numbers(self.root)), ["lost [promoted → 062]"])
        self.assertEqual(open_parked_items(text), [])                # no project context: unchanged


class DatedDeferral(_Fixture):
    def test_a_dated_owner_deferral_is_not_open(self):
        self._task(1, ["bench isolation [deferred: owner decision 2026-09-09 — until the Gemini seat exam]"])
        self.assertEqual(self._items(), {})
        self.assertEqual(set(self._items(open_only=False).values()), {"deferred"})

    def test_an_undated_deferral_stays_open(self):
        self._task(1, ["vague item [deferred: later]"])
        self.assertEqual(set(self._items().values()), {"open"})


class ParkedCommandOutput(_Fixture):
    def _cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-parked")
        return subprocess.run([sys.executable, "-m", "tasks.cli", "parked", *args], cwd=self.root, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=60).stdout

    def test_dangling_is_tagged_and_deferred_is_counted(self):
        self._task(1, ["lost [promoted → 062]",
                       "waiting [deferred: owner decision 2026-09-09 — until X]"])
        out = self._cli()
        self.assertIn("1 open parked item(s):", out)
        self.assertIn("lost [promoted → 062]  [dangling — task 062 does not exist]", out)
        self.assertIn("1 deferred by a dated owner decision", out)

    def test_zero_open_still_names_the_deferred(self):
        self._task(1, ["waiting [deferred: owner decision 2026-09-09 — until X]"])
        out = self._cli()
        self.assertIn("No open parked items.", out)
        self.assertIn("1 deferred by a dated owner decision", out)


if __name__ == "__main__":
    unittest.main()
