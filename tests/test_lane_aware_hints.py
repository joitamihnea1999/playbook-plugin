"""User-facing path hints name the RESOLVED lane, not the single-user layout.

Genesis-gauntlet finding (multi-user lanes' first live drive): on a repo with
`.agent/current_user`, `tasks list` printed `Task files:
.agent/tasks/<name>/task.md` — a path that does not exist there; the real
lane is `.agent/<user>/tasks/`. Same disease in every "No .agent/<file>
found" message whose file actually lives in the lane. The fix prints the
path the command RESOLVED (it already holds it), so:

  - multi-user repos see the lane path (`.agent/alice/...`), and
  - single-user repos are BYTE-UNCHANGED — the lane resolves to `.agent/`,
    reproducing the old literal exactly. The single-user controls here pin
    that, and tests/test_cli_dispatch.py's baseline markers pin the sibling
    messages (timeline/tagger/tag/log) in single-user mode independently.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLAYBOOK = _HERE.parent / "plugins" / "playbook"

_TASK = "# 001 - Seed\n\n## Status\nin_progress\n\n## Intent\nx\n"


def _run(proj: Path, *args) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_PLAYBOOK)
    env["PLAYBOOK_SESSION_ID"] = "pid-999999995"
    return subprocess.run(
        [sys.executable, "-m", "tasks.cli", *args],
        cwd=proj, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


class MultiUserHintsNameTheLane(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        proj = Path(self._tmp.name)
        (proj / ".agent").mkdir()
        (proj / ".agent" / "current_user").write_text("alice\n", encoding="utf-8")
        tdir = proj / ".agent" / "alice" / "tasks" / "001-seed"
        tdir.mkdir(parents=True)
        (tdir / "task.md").write_text(_TASK, encoding="utf-8")
        self.proj = proj

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_hint_prints_the_resolved_lane(self):
        r = _run(self.proj, "list")
        self.assertIn("Task files: .agent/alice/tasks/<name>/task.md", r.stdout)
        self.assertNotIn("Task files: .agent/tasks/", r.stdout)

    def test_missing_chat_log_names_the_lane_path(self):
        r = _run(self.proj, "log")
        self.assertIn(".agent/alice/chat_log.md not found", r.stderr)

    def test_missing_bash_history_names_the_lane_path(self):
        r = _run(self.proj, "timeline")
        self.assertIn("No .agent/alice/bash_history found.", r.stderr)

    def test_missing_lane_tasks_dir_names_the_lane(self):
        import shutil
        shutil.rmtree(self.proj / ".agent" / "alice" / "tasks")
        r = _run(self.proj, "list")
        self.assertIn("No .agent/alice/tasks/ directory found", r.stdout)


class SingleUserHintsAreByteUnchanged(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        proj = Path(self._tmp.name)
        tdir = proj / ".agent" / "tasks" / "001-seed"
        tdir.mkdir(parents=True)
        (tdir / "task.md").write_text(_TASK, encoding="utf-8")
        self.proj = proj

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_hint_exact_line(self):
        r = _run(self.proj, "list")
        self.assertIn(
            "Task files: .agent/tasks/<name>/task.md — activate with: "
            "tasks work <number>\n",
            r.stdout,
        )

    def test_missing_chat_log_exact_line(self):
        r = _run(self.proj, "log")
        self.assertIn("Error: .agent/chat_log.md not found", r.stderr)


if __name__ == "__main__":
    unittest.main()
