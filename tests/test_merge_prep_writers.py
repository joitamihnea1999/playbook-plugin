"""Writer-side tests for merge_prep.py (§2.3): the whole file shipped with no
tests, which is exactly why C2 (renumber corruption) sat unprotected. These pin
the pad-preserving renumber helper across widths and the task-ref rewriter's
output format.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.merge_prep import (  # noqa: E402
    _read_text_lossy, _renumbered_dir_name, _rewrite_task_refs,
)


class ReadTextLossy(unittest.TestCase):
    """N3 (1.5.17): a rewrite that decodes with errors='replace' loses non-UTF-8
    bytes. The behavior stays crash-safe (surrogateescape would just move the
    crash to encode sites), but the read now REPORTS lossiness so the caller can
    warn instead of silently corrupting."""

    def _write(self, data: bytes) -> Path:
        d = tempfile.mkdtemp()
        p = Path(d) / "chat_log.md"
        p.write_bytes(data)
        return p

    def test_clean_utf8_is_not_lossy(self):
        text, lossy = _read_text_lossy(self._write("hi ünïcode ✓\n".encode("utf-8")))
        self.assertFalse(lossy)
        self.assertIn("ünïcode", text)

    def test_non_utf8_bytes_flagged_lossy(self):
        # 0xFF is invalid UTF-8 (e.g. a stray cp1252 byte).
        text, lossy = _read_text_lossy(self._write(b"ok \xff bad\n"))
        self.assertTrue(lossy)
        self.assertIn("�", text)  # replacement char, never a crash


class RenumberedDirName(unittest.TestCase):
    def test_pad_width_table(self):
        cases = [
            # (old_name, new_num, expected) — pad width preserved, slug intact.
            ("002-feat-two", 3, "003-feat-two"),      # C2: not 302-...
            ("099-y", 100, "100-y"),                  # width holds at the boundary
            ("099-y", 1009, "1009-y"),                # natural growth past width
            ("001-a", 420, "420-a"),                  # not 42001-a
            ("020-x", 21, "021-x"),
            ("133-prepare-merge", 135, "135-prepare-merge"),
            ("7-nopad", 8, "8-nopad"),                # 1-digit prefix preserved
            ("no-leading-number", 5, "no-leading-number"),  # nothing to renumber
        ]
        for old, new, expected in cases:
            with self.subTest(old=old, new=new):
                self.assertEqual(_renumbered_dir_name(old, new), expected)


class RewriteTaskRefs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        self.agent = self.project / ".agent"
        (self.agent / "tasks").mkdir(parents=True)

    def _task(self, name, body):
        d = self.agent / "tasks" / name
        d.mkdir(parents=True)
        (d / "task.md").write_text(body, encoding="utf-8")
        return d / "task.md"

    def test_rewrites_t_refs_and_task_refs(self):
        tf = self._task("305-a", "See T2 and task 2 and G2:3.\n")
        _rewrite_task_refs(self.project, self.agent, {2: 305})
        out = tf.read_text(encoding="utf-8")
        self.assertIn("T305", out)
        self.assertIn("task 305", out)
        self.assertIn("G305:3", out)
        self.assertNotIn("T2 ", out)

    def test_descending_order_avoids_cascade(self):
        # T13 must not be rewritten as part of T133 (processed high-first).
        tf = self._task("400-b", "T13 and T133 are different.\n")
        _rewrite_task_refs(self.project, self.agent, {13: 200, 133: 300})
        out = tf.read_text(encoding="utf-8")
        self.assertIn("T200", out)
        self.assertIn("T300", out)
        self.assertNotIn("T13 ", out)
        self.assertNotIn("T133", out)

    def test_rewrites_current_state_in_dead_sessions(self):
        sess = self.agent / "sessions" / "pid-dead-000"
        sess.mkdir(parents=True)
        (sess / "current_state").write_text("2\n", encoding="utf-8")
        self._task("305-a", "body\n")
        _rewrite_task_refs(self.project, self.agent, {2: 305})
        self.assertEqual((sess / "current_state").read_text(encoding="utf-8").strip(),
                         "305")


if __name__ == "__main__":
    unittest.main()
