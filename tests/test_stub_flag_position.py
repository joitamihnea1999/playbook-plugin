#!/usr/bin/env python3
"""`tasks new` --stub flag is position-independent.

Batch-5 / gauntlet-155 UX wart: `tasks new feature name --stub` silently
swallowed the flag into the task's Intent text ("--stub" as intent) and
created a full template instead of a stub. The flag must work anywhere in
the argument list, and must never leak into Intent.

Run: python3 -m unittest tests.test_stub_flag_position
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

ENV = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-stubflag")


def _project() -> Path:
    proj = Path(tempfile.mkdtemp())
    (proj / ".agent" / "tasks").mkdir(parents=True)
    return proj


def _new(proj: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([sys.executable, "-m", "tasks.cli", "new", *args],
                          cwd=proj, env=ENV, capture_output=True, text=True,
                          timeout=60)


def _task_text(proj: Path) -> str:
    tf = next((proj / ".agent" / "tasks").glob("001-*/task.md"))
    return tf.read_text(encoding="utf-8")


class StubFlagPosition(unittest.TestCase):
    def test_trailing_stub_flag_makes_a_stub(self):
        proj = _project()
        r = _new(proj, "feature", "late-stub", "--stub")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = _task_text(proj)
        self.assertIn("<!-- stub:feature -->", text,
                      "trailing --stub did not produce a stub")
        self.assertNotIn("--stub", text,
                         "the flag leaked into the task body")

    def test_stub_flag_between_name_and_intent(self):
        proj = _project()
        r = _new(proj, "feature", "mid-stub", "--stub", "real intent words")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = _task_text(proj)
        self.assertIn("<!-- stub:feature -->", text)
        self.assertNotIn("--stub", text)

    def test_flag_first_still_works(self):
        proj = _project()
        r = _new(proj, "--stub", "feature", "classic-stub")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<!-- stub:feature -->", _task_text(proj))

    def test_no_flag_intent_text_unchanged(self):
        # Negative control: ordinary intent words must keep flowing into
        # ## Intent untouched, and no stub is minted.
        proj = _project()
        r = _new(proj, "feature", "plain-task", "fix", "the", "parser")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = _task_text(proj)
        self.assertNotIn("<!-- stub:", text)
        self.assertIn("fix the parser", text)


if __name__ == "__main__":
    unittest.main()
