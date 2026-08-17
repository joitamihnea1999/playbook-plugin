#!/usr/bin/env python3
"""`tasks recall` — cross-tier mind-map retrieval.

The invariants: an id fetches the node from BOTH tiers (main + overflow) and
labels each; a keyword LOCATES node ids across both files with AND semantics; a
fenced `[9]` is never a node; and the empty/missing cases degrade with a
pointer, never a crash.

Run: python3 tests/test_recall.py
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.mindmap import cmd_recall  # noqa: E402

MAIN = (
    "# Mind Map\n\n> header\n\n"
    "[1] **Overview** - the entry point, see [2] and [6].\n\n"
    "[2] **Storage** - persistence in src/store.py, summarized ↗ see overflow [1].\n\n"
    "[6] **Auth Policy** - identity and policy gating live in src/auth.py [1].\n\n"
    "```\n[9] this is a fenced example, not a node\n```\n\n"
)
OVERFLOW = (
    "# Overflow\n\n"
    "[2] **Storage** - the FULL detail: StorageManager, cache eviction, the JSON "
    "format choice, backup rotation — all the prose trimmed out of the main summary.\n\n"
    "[6] **Auth Policy** - full detail: the policy engine, gate classifier, the "
    "auth token refresh path.\n\n"
)


class Recall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".agent" / "tasks").mkdir(parents=True)
        (self.root / "MIND_MAP.md").write_text(MAIN, encoding="utf-8")
        self._cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def _overflow(self):
        (self.root / "MIND_MAP_OVERFLOW.md").write_text(OVERFLOW, encoding="utf-8")

    def _run(self, *args):
        out, err, code = io.StringIO(), io.StringIO(), 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cmd_recall(list(args))
        except SystemExit as e:
            code = e.code or 0
        return code, out.getvalue(), err.getvalue()

    # --- node-id mode ------------------------------------------------------

    def test_id_prints_both_tiers(self):
        self._overflow()
        code, out, _ = self._run("2")
        self.assertEqual(code, 0)
        self.assertIn("from MIND_MAP.md", out)
        self.assertIn("summarized ↗", out)                 # the main summary
        self.assertIn("MIND_MAP_OVERFLOW.md (fuller detail)", out)
        self.assertIn("StorageManager, cache eviction", out)  # the overflow detail

    def test_id_main_only_when_no_overflow_file(self):
        code, out, _ = self._run("6")
        self.assertEqual(code, 0)
        self.assertIn("from MIND_MAP.md", out)
        self.assertNotIn("OVERFLOW", out)

    def test_id_present_in_main_absent_in_overflow_says_so(self):
        self._overflow()   # overflow has [2],[6] but not [1]
        code, out, _ = self._run("1")
        self.assertEqual(code, 0)
        self.assertIn("from MIND_MAP.md", out)
        self.assertIn("no [1] in overflow", out)

    def test_id_absent_everywhere(self):
        code, out, _ = self._run("99")
        self.assertEqual(code, 0)
        self.assertIn("No node [99]", out)

    def test_fenced_node_is_not_recallable(self):
        code, out, _ = self._run("9")
        self.assertIn("No node [9]", out)

    # --- keyword mode ------------------------------------------------------

    def test_keyword_locates_across_both_files(self):
        self._overflow()
        code, out, _ = self._run("auth")
        self.assertEqual(code, 0)
        self.assertIn("MIND_MAP.md:", out)
        self.assertIn("[6] Auth Policy", out)
        self.assertIn("MIND_MAP_OVERFLOW.md", out)
        self.assertIn("matched", out)

    def test_keyword_and_semantics(self):
        # "policy" appears in [6] only; "storage" in [2] only → AND matches none.
        code, out, _ = self._run("policy", "storage")
        self.assertIn("No node matched", out)
        # Each alone matches its node.
        _, out6, _ = self._run("policy")
        self.assertIn("[6] Auth Policy", out6)

    def test_keyword_no_match(self):
        code, out, _ = self._run("nonexistentterm")
        self.assertEqual(code, 0)
        self.assertIn("No node matched", out)

    # --- guards ------------------------------------------------------------

    def test_no_arg(self):
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("'recall' requires a node id or keyword", err)

    def test_no_map(self):
        (self.root / "MIND_MAP.md").unlink()
        code, _, err = self._run("2")
        self.assertEqual(code, 1)
        self.assertIn("MIND_MAP.md not found", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
