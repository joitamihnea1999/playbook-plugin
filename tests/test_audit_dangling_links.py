#!/usr/bin/env python3
"""Dangling mind-map link audit check + its negative controls.

A `[N]` link to a node that isn't defined anywhere is a dead end the agent
follows. The check must fire on exactly that and stay quiet on everything that
merely looks like it — fenced examples, markdown checkboxes, version tags, and
range tokens are NOT node links.

Run: python3 tests/test_audit_dangling_links.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.audit import check_mindmap_dangling_links  # noqa: E402


class DanglingLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _map(self, body, audit=None):
        (self.d / "MIND_MAP.md").write_text(body, encoding="utf-8")
        if audit is not None:
            (self.d / ".agent").mkdir(exist_ok=True)
            (self.d / ".agent" / "config.json").write_text(
                json.dumps({"audit": audit}), encoding="utf-8")
        return check_mindmap_dangling_links(self.d)

    def test_clean_when_all_links_resolve(self):
        r = self._map(
            "[1] **A** - see [2].\n\n[2] **B** - back to [1].\n")
        self.assertEqual(r["status"], "clean", r["output"])

    def test_fires_and_names_source_and_target(self):
        r = self._map(
            "[1] **A** - see [2] and [47].\n\n[2] **B** - fine [1].\n")
        self.assertEqual(r["status"], "findings", r["output"])
        self.assertIn("[47]", r["output"])
        self.assertIn("node [1]", r["output"])   # the source is named

    def test_fenced_link_is_not_counted(self):
        r = self._map(
            "[1] **A** - see [2].\n\n```\nfollow [99] here in an example\n```\n\n"
            "[2] **B** - fine.\n")
        self.assertEqual(r["status"], "clean", r["output"])

    def test_markdown_checkbox_is_not_a_link(self):
        r = self._map("[1] **A** - a node.\n- [ ] a checkbox\n- [x] done\n")
        self.assertEqual(r["status"], "clean", r["output"])

    def test_version_and_range_tokens_are_not_links(self):
        r = self._map(
            "[1] **A** - shipped in [1.5.0], routing nodes [1-5] link here.\n")
        # [1.5.0] and [1-5] must not register as links to undefined nodes.
        self.assertEqual(r["status"], "clean", r["output"])

    def test_preamble_dangling_link_labeled_not_none(self):
        # A dangling [N] before the first node was reported as "node [None]";
        # it should read as preamble (1.5.26 audit finding).
        r = self._map("intro links to [42]\n\n[1] **A** - a node.\n")
        self.assertEqual(r["status"], "findings")
        self.assertIn("preamble", r["output"])
        self.assertNotIn("[None]", r["output"])

    def test_no_map_returns_none(self):
        self.assertIsNone(check_mindmap_dangling_links(self.d))

    def test_severity_advisory_by_default(self):
        r = self._map("[1] **A** - dead [9].\n")
        self.assertEqual(r["severity"], "advisory")

    def test_severity_configurable_to_error(self):
        r = self._map("[1] **A** - dead [9].\n",
                      audit={"dangling_links_severity": "error"})
        self.assertEqual(r["severity"], "error")
        self.assertEqual(r["status"], "findings")


if __name__ == "__main__":
    unittest.main(verbosity=2)
