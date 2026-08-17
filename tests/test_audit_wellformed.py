#!/usr/bin/env python3
"""Mind-map well-formedness audit + negative controls.

Structural defects in how the map is WRITTEN, caught mechanically: duplicate
node ids, nodes missing a **bold title**, and unreachable islands (a non-routing
node nothing links to). Each check must fire on exactly its defect and stay
quiet on a clean map — including the exemptions (routing nodes are entry points,
a node's own definition token is not a link to itself).

Run: python3 tests/test_audit_wellformed.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.audit import check_mindmap_wellformed  # noqa: E402

# 6 nodes: [1]-[5] routing, [6] a leaf linked from the overview. Well-formed.
CLEAN = (
    "[1] **Overview** - entry, see [2] [3] [4] [5] and leaf [6].\n"
    "[2] **A** - back to [1].\n"
    "[3] **B** - back to [1].\n"
    "[4] **C** - back to [1].\n"
    "[5] **D** - back to [1].\n"
    "[6] **Leaf** - detail, linked from [1].\n"
)


class Wellformed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, body, audit=None):
        (self.d / "MIND_MAP.md").write_text(body, encoding="utf-8")
        if audit is not None:
            (self.d / ".agent").mkdir(exist_ok=True)
            (self.d / ".agent" / "config.json").write_text(
                json.dumps({"audit": audit}), encoding="utf-8")
        return check_mindmap_wellformed(self.d)

    def test_clean_map_is_clean(self):
        self.assertEqual(self._run(CLEAN)["status"], "clean", self._run(CLEAN)["output"])

    def test_duplicate_id(self):
        r = self._run(CLEAN + "[6] **Leaf Again** - a second [6] [1].\n")
        self.assertEqual(r["status"], "findings")
        self.assertIn("duplicate node id [6]", r["output"])

    def test_missing_bold_title(self):
        # [7] is linked (so not unreachable) but has no **bold** title.
        body = CLEAN.replace("leaf [6].", "leaf [6] and [7].") + "[7] plain, no bold [1].\n"
        r = self._run(body)
        self.assertIn("node [7] has no **bold title**", r["output"])
        self.assertNotIn("unreachable", r["output"])   # it IS reachable

    def test_unreachable_island(self):
        r = self._run(CLEAN + "[8] **Island** - nothing links here [1].\n")
        self.assertIn("node [8] is unreachable", r["output"])

    def test_routing_nodes_exempt_from_unreachable(self):
        # [1] is never linked TO (it's the overview) — must NOT be flagged.
        self.assertNotIn("[1] is unreachable", self._run(CLEAN)["output"])

    def test_self_definition_is_not_a_link(self):
        # A node whose only [6] occurrence is its own definition is unreachable.
        body = ("[1] **Overview** - see [2] [3] [4] [5].\n"
                "[2] **A** - [1].\n[3] **B** - [1].\n[4] **C** - [1].\n[5] **D** - [1].\n"
                "[6] **Orphan** - mentions only itself, links out to [1].\n")
        r = self._run(body)
        self.assertIn("node [6] is unreachable", r["output"])

    def test_no_map_returns_none(self):
        self.assertIsNone(check_mindmap_wellformed(self.d))

    def test_severity_advisory_default_configurable(self):
        self.assertEqual(self._run(CLEAN)["severity"], "advisory")
        r = self._run(CLEAN + "[8] **Island** - [1].\n",
                      audit={"wellformed_severity": "error"})
        self.assertEqual(r["severity"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
