"""`tasks prepare-merge` must renumber colliding task directories
pad-preservingly, and its `--dry-run` preview must predict EXACTLY what the
real run does (verification-report-1.5.9 C2).

C2: `_rename_colliding_tasks` computed
`new_name = str(new_num) + old_name[len(str(old_num)):]`, slicing by the
*unpadded* number's width. On the canonical zero-padded layout that corrupts
every task numbered < 100 (the normal case): `002-feat-two` → `30202-feat-two`,
`099-y` → `1009-y`. The resulting directory is unreachable by the `NNN-*` glob
every consumer uses, while the H1 inside says the intended number. Worse, the
`--dry-run` preview at :115 used a *different* computation (`str.replace`), so
the preview lied about what the command would do.

This drives the real CLI against a real two-branch git repo (the report's
repro), and pins BOTH the pad-preserving rename AND preview == action.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"

TASK_MD = """# {num} - {slug}

## Status
pending

## Work Plan
- [ ] a gate
"""


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


class MergePrepRenumberBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        self.project.mkdir(parents=True)
        _git(self.project, "init", "-q", "-b", "main")
        _git(self.project, "config", "user.email", "t@t")
        _git(self.project, "config", "user.name", "t")

    def _add_task(self, num, slug):
        d = self.project / ".agent" / "tasks" / f"{num}-{slug}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "task.md").write_text(TASK_MD.format(num=num, slug=slug), encoding="utf-8")

    def _commit(self, msg):
        _git(self.project, "add", "-A")
        _git(self.project, "commit", "-q", "-m", msg)

    def run_tasks(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["PLAYBOOK_SESSION_ID"] = "pid-mergeprep-test"
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", *args],
            cwd=self.project, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    def _setup_collision(self, num, slug_target, slug_current):
        """A task number `num` created independently on main and on a branch —
        the classic collision prepare-merge exists to resolve."""
        # Base commit (no task num yet).
        (self.project / "README").write_text("x", encoding="utf-8")
        self._commit("base")
        # main gets NUM-<slug_target>
        self._add_task(num, slug_target)
        self._commit(f"main task {num}")
        # branch off base, add NUM-<slug_current>
        _git(self.project, "checkout", "-q", "-b", "feature", "HEAD~1")
        self._add_task(num, slug_current)
        self._commit(f"feature task {num}")


class TestRenumberPadPreserving(MergePrepRenumberBase):
    def test_renumber_keeps_padding_and_is_glob_reachable(self):
        # A sub-100 task number: the corruption case.
        self._setup_collision("002", "feat-main", "feat-two")
        r = self.run_tasks("prepare-merge", "--target", "main")
        self.assertEqual(r.returncode, 0, f"prepare-merge failed: {r.stderr}")

        tasks_dir = self.project / ".agent" / "tasks"
        names = sorted(d.name for d in tasks_dir.iterdir() if d.is_dir())
        # main holds only task 002, so the collision renumbers to 003 —
        # pad-preserving. The bug produced "302-feat-two" (slice by unpadded
        # width). Assert the EXACT pad-preserving name, not just "3 digits"
        # (302 is coincidentally 3 digits too).
        self.assertIn("003-feat-two", names,
                      f"renumber corrupted the name (dirs: {names})")
        self.assertNotIn("302-feat-two", names, "C2 corruption present")
        # Glob-reachable by the NNN-* pattern every consumer uses.
        self.assertTrue(list(tasks_dir.glob("003-*")),
                        "003-feat-two is unreachable by the NNN-* glob")

    def test_dry_run_preview_matches_action(self):
        self._setup_collision("002", "feat-main", "feat-two")
        dry = self.run_tasks("prepare-merge", "--target", "main", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        # Extract the previewed new dir name.
        import re
        m = re.search(r"rename \S+ → (\S+)", dry.stdout)
        assert m is not None, f"no rename preview line: {dry.stdout!r}"
        previewed = m.group(1)

        # Now the real run.
        self.assertEqual(self.run_tasks("prepare-merge", "--target", "main").returncode, 0)
        tasks_dir = self.project / ".agent" / "tasks"
        names = [d.name for d in tasks_dir.iterdir() if d.is_dir()]
        self.assertIn(previewed, names,
                      f"preview {previewed!r} lied about the action (dirs: {names})")


if __name__ == "__main__":
    unittest.main()
