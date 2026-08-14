#!/usr/bin/env python3
"""F23 — mindmap-sync degrades gracefully on a single-map project.

Genesis-gauntlet finding: the merge skill's Step 6 mandates `mindmap-sync`
(read-only, then --fix), but a young project has no MIND_MAP_OVERFLOW.md yet
and the command hard-errored (rc 1, "Error: ... not found") — stranding a
faithful merge run on exactly the shape every project starts with.
ref-integrity.py already degrades ("overflow checks skipped"); sync must too.

Run: python3 -m unittest tests.test_mindmap_sync_no_overflow
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
ENV = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-f23")


def _run(proj: Path, *args: str):
    return subprocess.run([sys.executable, "-m", "tasks.cli", "mindmap-sync", *args],
                          cwd=proj, env=ENV, capture_output=True, text=True, timeout=60)


class NoOverflowGracefulDegrade(unittest.TestCase):
    def _proj(self, with_map=True) -> Path:
        d = Path(tempfile.mkdtemp())
        (d / ".agent" / "tasks").mkdir(parents=True)
        if with_map:
            (d / "MIND_MAP.md").write_text("[1] **X** - a node.\n", encoding="utf-8")
        return d

    def test_missing_overflow_is_a_clean_note(self):
        for flags in ((), ("--fix",)):
            r = _run(self._proj(), *flags)
            self.assertEqual(r.returncode, 0,
                             f"{flags}: single-map project must not error: {r.stderr}")
            self.assertIn("nothing to sync", r.stdout)

    def test_missing_main_map_still_errors(self):
        # Negative control: no MIND_MAP.md at all is a real error, unchanged.
        r = _run(self._proj(with_map=False))
        self.assertEqual(r.returncode, 1)
        self.assertIn("MIND_MAP.md not found", r.stderr)


if __name__ == "__main__":
    unittest.main()
