#!/usr/bin/env python3
"""Point tests for `resolve_panel_quorum` / `_parse_quorum` (report C4 / P7).

The gap this closes: a panel reported `N/M succeeded` and exited 0 at any N — a
1/7 panel and a 7/7 panel were the same exit code. resolve_panel_quorum turns
the count into a required-success threshold so the caller can decide PASS/FAIL.

Invariants:
  * default is strict majority of launched judges (4 of 7, 1 of 1);
  * "all" requires every launched judge; a fraction rounds up;
  * an absolute int is NOT clamped up — requiring 7 when 4 launched stays 7 so a
    degraded panel fails rather than silently passing;
  * a garbage value never raises — it warns and falls back to majority.

Pure stdlib unittest. Run: python3 tests/test_panel_quorum.py
"""
import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.core import _parse_quorum, resolve_panel_quorum  # noqa: E402


class ParseQuorum(unittest.TestCase):
    def test_majority(self):
        self.assertEqual(_parse_quorum("majority", 7), 4)
        self.assertEqual(_parse_quorum("majority", 1), 1)
        self.assertEqual(_parse_quorum("majority", 6), 4)

    def test_all(self):
        self.assertEqual(_parse_quorum("all", 7), 7)
        self.assertEqual(_parse_quorum("all", 1), 1)

    def test_absolute_int_not_clamped_up(self):
        # Requiring a full panel when only 4 launched must FAIL (7 > 4), not hide it.
        self.assertEqual(_parse_quorum(7, 4), 7)
        self.assertEqual(_parse_quorum("3", 7), 3)

    def test_fraction_rounds_up(self):
        self.assertEqual(_parse_quorum(0.5, 7), 4)      # ceil(3.5)
        self.assertEqual(_parse_quorum(0.66, 7), 5)     # ceil(4.62)
        self.assertEqual(_parse_quorum(1.0, 7), 7)

    def test_rejects_garbage(self):
        for bad in (True, 0, -1, 1.5, "banana", "0.0", None):
            with self.assertRaises((ValueError, TypeError)):
                _parse_quorum(bad, 7)


class ResolveQuorum(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("PLAYBOOK_PANEL_QUORUM", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["PLAYBOOK_PANEL_QUORUM"] = self._saved
        else:
            os.environ.pop("PLAYBOOK_PANEL_QUORUM", None)

    def test_default_majority_no_config(self, tmp=None):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resolve_panel_quorum(Path(d), 7), 4)

    def test_env_overrides(self):
        import tempfile
        os.environ["PLAYBOOK_PANEL_QUORUM"] = "all"
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resolve_panel_quorum(Path(d), 5), 5)

    def test_config_value(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            agent = Path(d) / ".agent"
            agent.mkdir()
            (agent / "config.json").write_text(json.dumps({"panel_quorum": 3}))
            self.assertEqual(resolve_panel_quorum(Path(d), 7), 3)

    def test_bad_config_falls_back_to_majority(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            agent = Path(d) / ".agent"
            agent.mkdir()
            (agent / "config.json").write_text(json.dumps({"panel_quorum": "banana"}))
            self.assertEqual(resolve_panel_quorum(Path(d), 7), 4)


if __name__ == "__main__":
    unittest.main()
