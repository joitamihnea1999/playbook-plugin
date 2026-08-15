#!/usr/bin/env python3
"""I6 (verification-report-1.5.9): the merge-artifacts audit sweep must FAIL
CLOSED (ERROR) when its scan cannot complete — not report CLEAN.

`find … | grep .` took the pipeline's status from grep, so a `find` that errors
mid-scan (permission-denied dir) yielded rc 1 → classified CLEAN, defeating the
module's own "a measuring tool that can't run cannot report clean" doctrine. The
grep-based conflict-markers sweep correctly ERRORs on the same fixture; this one
did not. (pipefail alone does NOT fix it — find's error exit is 1, colliding
with grep's clean exit 1.)
"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.audit import DEFAULT_SWEEPS, run_sweep  # noqa: E402


def sweep_by_name(name):
    return next(s for s in DEFAULT_SWEEPS if s["name"] == name)


class AuditSweepFailClosed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)

    def _make_unreadable_subdir(self):
        sub = self.project / "locked"
        sub.mkdir()
        (sub / "keep").write_text("x", encoding="utf-8")
        os.chmod(sub, 0)
        # Restore perms at teardown so TemporaryDirectory can clean up.
        self.addCleanup(lambda: os.chmod(sub, stat.S_IRWXU))

    def test_merge_artifacts_errors_on_incomplete_scan(self):
        self._make_unreadable_subdir()
        result = run_sweep(sweep_by_name("merge-artifacts"), self.project)
        self.assertEqual(result["status"], "error",
                         f"find error mis-reported as {result['status']} "
                         f"(rc={result['rc']}) — false clean (I6)")

    def test_conflict_markers_errors_on_incomplete_scan(self):
        # Control: the grep-based sweep already ERRORs on the same fixture.
        self._make_unreadable_subdir()
        result = run_sweep(sweep_by_name("conflict-markers"), self.project)
        self.assertEqual(result["status"], "error")

    def test_merge_artifacts_clean_when_scan_completes(self):
        # Negative control: a fully-readable, artifact-free tree is CLEAN.
        (self.project / "a.py").write_text("x=1\n", encoding="utf-8")
        result = run_sweep(sweep_by_name("merge-artifacts"), self.project)
        self.assertEqual(result["status"], "clean",
                         f"clean tree mis-reported as {result['status']}")

    def test_merge_artifacts_finds_real_artifacts(self):
        # Negative control: an actual .orig file is FINDINGS.
        (self.project / "x.orig").write_text("conflict\n", encoding="utf-8")
        result = run_sweep(sweep_by_name("merge-artifacts"), self.project)
        self.assertEqual(result["status"], "findings",
                         f"real .orig not found: {result['status']}")


if __name__ == "__main__":
    unittest.main()
