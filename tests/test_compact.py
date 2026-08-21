#!/usr/bin/env python3
"""`tasks compact <N>` — moving agent-marked cold narrative out of task.md.

The invariants that make it SAFE to automate a move out of the execution trace:
a clean move is verbatim and reversible-by-reading (archive gets exactly what
task.md lost, plus a pointer); and every unsafe shape — unmatched/nested
markers, a gate checkbox, a pin, a protected heading inside the block — aborts
the WHOLE run and writes nothing, so a mismark fails loud instead of amputating.

Run: python3 tests/test_compact.py
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
from tasks.compact import cmd_compact  # noqa: E402

BODY = """# 012 - Example

## Status
done

## Intent
Ship the thing.

## Work Plan
- [x] build it — done, works
- [x] test it — 12 tests green

## Implementation Review
- [x] Triage findings
<!-- archive:start -->
### Round 1 findings
The judge flagged three things; all triaged and rejected with rationale.
Long narrative that is cold now and only bloats the review keyhole.
<!-- archive:end -->

## Parked
Nothing.
"""


class Compact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task_dir = self.root / ".agent" / "tasks" / "012-example"
        self.task_dir.mkdir(parents=True)
        self.task_md = self.task_dir / "task.md"
        self.task_md.write_text(BODY, encoding="utf-8")
        self._cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def _run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cmd_compact(list(args))
        except SystemExit as e:
            code = e.code or 0
        return code, out.getvalue(), err.getvalue()

    # --- clean move --------------------------------------------------------

    def test_moves_block_verbatim_and_leaves_a_pointer(self):
        code, out, err = self._run("12")
        self.assertEqual(code, 0, err)
        task = self.task_md.read_text(encoding="utf-8")
        archive = (self.task_dir / "task-archive.md").read_text(encoding="utf-8")
        # The narrative is gone from task.md, present verbatim in the archive.
        self.assertNotIn("Round 1 findings", task)
        self.assertNotIn("archive:start", task)
        self.assertIn("Round 1 findings", archive)
        self.assertIn("triaged and rejected with rationale", archive)
        # A pointer replaces it, and the gates are untouched.
        self.assertIn("compacted", task)
        self.assertIn("- [x] build it — done, works", task)
        self.assertIn("## Parked", task)

    def test_zero_padding_accepts_bare_number(self):
        code, _, err = self._run("012")
        self.assertEqual(code, 0, err)

    def test_second_compact_appends_not_overwrites(self):
        self._run("12")
        # Add a second cold block and compact again.
        task = self.task_md.read_text(encoding="utf-8")
        task = task.replace(
            "## Parked",
            "<!-- archive:start -->\nsecond cold block\n<!-- archive:end -->\n\n## Parked")
        self.task_md.write_text(task, encoding="utf-8")
        self._run("12")
        archive = (self.task_dir / "task-archive.md").read_text(encoding="utf-8")
        self.assertIn("Round 1 findings", archive)   # first still there
        self.assertIn("second cold block", archive)  # second appended

    def test_dry_run_writes_nothing(self):
        code, out, _ = self._run("12", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out)
        self.assertIn("Round 1 findings", self.task_md.read_text(encoding="utf-8"))
        self.assertFalse((self.task_dir / "task-archive.md").exists())

    # --- guards ------------------------------------------------------------

    def test_no_task_number(self):
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("'compact' requires a task number", err)

    def test_unknown_task(self):
        code, _, err = self._run("99")
        self.assertEqual(code, 1)
        self.assertIn("no task 099", err)

    def test_no_markers_is_a_noop(self):
        self.task_md.write_text(BODY.replace(
            "<!-- archive:start -->", "").replace("<!-- archive:end -->", ""),
            encoding="utf-8")
        code, out, _ = self._run("12")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to compact", out)

    def _corrupt_and_run(self, body):
        self.task_md.write_text(body, encoding="utf-8")
        return self._run("12")

    def test_unclosed_start_aborts(self):
        code, _, err = self._corrupt_and_run(
            BODY.replace("<!-- archive:end -->", ""))
        self.assertEqual(code, 1)
        self.assertIn("unclosed", err)
        self.assertIn("Round 1 findings", self.task_md.read_text(encoding="utf-8"))

    def test_stray_end_aborts(self):
        code, _, err = self._corrupt_and_run(
            BODY.replace("<!-- archive:start -->", ""))
        self.assertEqual(code, 1)
        self.assertIn("no matching", err)

    def test_nested_start_aborts(self):
        code, _, err = self._corrupt_and_run(BODY.replace(
            "### Round 1 findings", "<!-- archive:start -->\n### Round 1 findings"))
        self.assertEqual(code, 1)
        self.assertIn("nested", err)

    def test_block_with_gate_is_refused(self):
        code, _, err = self._corrupt_and_run(BODY.replace(
            "### Round 1 findings", "- [ ] a real gate hiding in the block"))
        self.assertEqual(code, 1)
        self.assertIn("gate checkbox", err)
        self.assertIn("a real gate", self.task_md.read_text(encoding="utf-8"))

    def test_block_with_pin_is_refused(self):
        code, _, err = self._corrupt_and_run(BODY.replace(
            "### Round 1 findings", "<!-- pin -->\n### Round 1 findings"))
        self.assertEqual(code, 1)
        self.assertIn("pin", err)

    def test_block_with_protected_heading_is_refused(self):
        code, _, err = self._corrupt_and_run(BODY.replace(
            "### Round 1 findings", "## Intent\nsomething precious"))
        self.assertEqual(code, 1)
        self.assertIn("protected section heading", err)
        self.assertIn("something precious", self.task_md.read_text(encoding="utf-8"))


    # --- 1.5.26 hardening (audit findings) --------------------------------

    def test_crlf_task_md_is_preserved_byte_for_byte(self):
        crlf = ("# 012\r\nlive line\r\n<!-- archive:start -->\r\n"
                "cold1\r\ncold2\r\n<!-- archive:end -->\r\ntail\r\n")
        self.task_md.write_text(crlf, encoding="utf-8", newline="")
        code, _, err = self._run("12")
        self.assertEqual(code, 0, err)
        with self.task_md.open(encoding="utf-8", newline="") as fh:
            after = fh.read()
        # Unmarked lines keep their CRLF (read_text-based code folded them to LF).
        self.assertIn("live line\r\n", after)
        self.assertIn("tail\r\n", after)
        self.assertNotIn("live line\n\r", after)
        # And the archived block is byte-verbatim CRLF.
        with (self.task_dir / "task-archive.md").open(encoding="utf-8", newline="") as fh:
            arch = fh.read()
        self.assertIn("cold1\r\ncold2\r\n", arch)

    def test_markers_inside_a_fence_are_ignored(self):
        body = ("# 012\n- [x] gate\n```\n<!-- archive:start -->\n"
                "an example of the ritual\n<!-- archive:end -->\n```\n")
        self.task_md.write_text(body, encoding="utf-8")
        code, out, _ = self._run("12")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to compact", out)   # the fenced markers are not real
        self.assertIn("an example of the ritual", self.task_md.read_text(encoding="utf-8"))
        self.assertFalse((self.task_dir / "task-archive.md").exists())

    def test_empty_block_is_skipped_not_hollow_archived(self):
        body = ("# 012\n- [x] gate\n<!-- archive:start -->\n<!-- archive:end -->\n")
        self.task_md.write_text(body, encoding="utf-8")
        code, out, _ = self._run("12")
        self.assertEqual(code, 0)
        self.assertIn("empty", out)
        self.assertFalse((self.task_dir / "task-archive.md").exists())

    def test_write_failure_rolls_back_archive_and_exits_clean(self):
        # Force the atomic task.md write to fail AFTER the archive append; the
        # block must not be left in the archive (else a retry double-appends),
        # task.md must be untouched, and there must be no traceback.
        from unittest import mock
        import tasks.atomic as A  # the atomic write now lives in the primitive
        with mock.patch.object(A.os, "replace", side_effect=OSError("boom")):
            code, _, err = self._run("12")
        self.assertEqual(code, 1)
        self.assertIn("rolled back", err)
        self.assertNotIn("Traceback", err)
        # task.md still has the block; archive was rolled back (never created).
        self.assertIn("Round 1 findings", self.task_md.read_text(encoding="utf-8"))
        self.assertFalse((self.task_dir / "task-archive.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
