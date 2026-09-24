#!/usr/bin/env python3
"""bash-log.sh logs what the agent ran — not every loop iteration, not the statusline (PLAN S5b).

The DEBUG trap fires for every simple command of every non-interactive bash in a
playbook project. On 2026-09-21 that meant 195,168 x 3 rows of loop iterations and
~40 statusline assignments per render (~710 renders a day), and the history reached
136 MB. The logger now writes each distinct command text once per shell process,
installs no trap in a shell that exports PLAYBOOK_NO_BASHLOG=1 or runs a script named
statusline, and rotates a history past 50 MB. Every probe runs the REAL bash-log.sh
through BASH_ENV, the way the harness does.

Run: python3 -m unittest tests.test_bash_log_scope
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._bashcheck import bash_or_skip

BL = Path(__file__).resolve().parent.parent / "plugins" / "playbook" / "scripts" / "bash-log.sh"


class BashLogScope(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".agent" / "tasks").mkdir(parents=True)
        self.hist = self.proj / ".agent" / "bash_history"

    def _bash(self, script, extra_env=None, argv=None):
        env = dict(os.environ, BASH_ENV=str(BL))
        env.pop("PLAYBOOK_NO_BASHLOG", None)
        env.update(extra_env or {})
        cmd = argv or [bash_or_skip(), "-c", script]
        return subprocess.run(cmd, cwd=self.proj, env=env, capture_output=True, text=True, timeout=60)

    def _lines(self):
        return self.hist.read_text(encoding="utf-8", errors="replace").splitlines() if self.hist.exists() else []

    def test_a_loop_logs_each_command_text_once(self):
        self._bash("for i in 1 2 3 4 5; do echo $i >/dev/null; done; echo done >/dev/null")
        cmds = [ln.split(" | AGENT | ", 1)[1] for ln in self._lines()]
        # Exactly the header, the body ONCE, and the next command (panel r1 P9:
        # `<= 3` + one assertIn also passed with the loop body dropped).
        self.assertEqual(cmds, ["for i in 1 2 3 4 5", "echo $i > /dev/null", "echo done > /dev/null"])

    def test_distinct_commands_are_all_logged(self):
        self._bash("echo one >/dev/null; echo two >/dev/null; echo three >/dev/null")
        text = "\n".join(self._lines())
        for w in ("one", "two", "three"):
            self.assertIn(f"echo {w}", text)

    def test_a_shell_that_opts_out_logs_nothing(self):
        self._bash("echo statusline-shaped >/dev/null", {"PLAYBOOK_NO_BASHLOG": "1"})
        self.assertNotIn("statusline-shaped", "\n".join(self._lines()))
        self._bash("echo opted-in >/dev/null", {"PLAYBOOK_NO_BASHLOG": "0"})
        self.assertIn("opted-in", "\n".join(self._lines()))

    def test_a_script_named_statusline_is_not_logged(self):
        script = self.proj / "statusline.sh"
        script.write_text("#!/bin/bash\necho by-name >/dev/null\n", encoding="utf-8")
        script.chmod(0o755)
        self._bash("", argv=[bash_or_skip(), str(script)])
        self.assertNotIn("by-name", "\n".join(self._lines()))

    def test_a_statusline_named_by_a_backslash_path_is_not_logged(self):
        # Windows lane, CI 36000444073: `bash C:\...\statusline.sh` leaves a
        # backslash path in $0, which `${0##*/}` does not strip. The same string
        # reaches the logger here as a file NAME containing a backslash (legal on
        # POSIX); on Windows it is a real subdirectory.
        rel = "sub\\statusline.sh"
        if os.name == "nt":
            (self.proj / "sub").mkdir()
            script = self.proj / "sub" / "statusline.sh"
        else:
            script = self.proj / rel
        script.write_bytes(b"#!/bin/bash\necho by-backslash-path >/dev/null\n")
        self._bash("", argv=[bash_or_skip(), rel])
        self.assertNotIn("by-backslash-path", "\n".join(self._lines()))

    def test_a_directory_named_statusline_does_not_hide_its_scripts(self):
        # Panel r1 P8: `*\\statusline-*` also matched a DIRECTORY component
        # (`w\statusline-tests\run.sh`), silently dropping every command run
        # from that tree. Only the script's own name counts.
        rel = "w\\statusline-tests\\run.sh"
        if os.name == "nt":
            (self.proj / "w" / "statusline-tests").mkdir(parents=True)
            script = self.proj / "w" / "statusline-tests" / "run.sh"
        else:
            script = self.proj / rel
        script.write_bytes(b"echo in-a-statusline-dir >/dev/null\n")
        self._bash("", argv=[bash_or_skip(), rel])
        self.assertIn("in-a-statusline-dir", "\n".join(self._lines()))

    def test_a_repeated_lifecycle_command_is_logged_each_time(self):
        # Panel r1 P5: `tasks work 7; tasks work 8; tasks work 7` in one shell
        # lost the second activation to the dedupe, and retro/timeline windows
        # are built from these lines. `tasks …` commands are never deduped.
        bindir = self.proj / "bin"
        bindir.mkdir()
        stub = bindir / "tasks"
        stub.write_bytes(b"#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        path = str(bindir) + os.pathsep + os.environ.get("PATH", "")
        self._bash("tasks work 7; tasks work 8; tasks work 7", {"PATH": path})
        work = [ln.split(" | AGENT | ", 1)[1] for ln in self._lines() if " | AGENT | tasks work" in ln]
        self.assertEqual(work, ["tasks work 7", "tasks work 8", "tasks work 7"])

    def test_a_loop_that_only_mentions_the_task_dir_is_still_deduped(self):
        # Panel r3 (grok): the r2 exemption (`*tasks*`) also exempted any loop
        # whose body names `.agent/tasks`, writing every iteration. Only
        # activation-shaped text (`tasks … work` / `tasks … new`) is exempt.
        self._bash("for i in 1 2 3; do echo .agent/tasks/x >/dev/null; done")
        body = [ln for ln in self._lines() if ln.endswith("| AGENT | echo .agent/tasks/x > /dev/null")]
        self.assertEqual(len(body), 1, self._lines())

    def test_a_quoted_tasks_path_is_never_deduped(self):
        # Panel r2 (sol-high, grok): the exemption matched only a bare `tasks `
        # or `/tasks `, so `"$B/tasks" work 7; … 8; … 7` lost the second 7. Any
        # command text containing `tasks` is now exempt (logging more is the
        # safe direction).
        bindir = self.proj / "b i n"
        bindir.mkdir()
        stub = bindir / "tasks"
        stub.write_bytes(b"#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        self._bash('"$B/tasks" work 7; "$B/tasks" work 8; "$B/tasks" work 7', {"B": str(bindir)})
        work = [ln.split(" | AGENT | ", 1)[1] for ln in self._lines() if '/tasks" work' in ln]
        self.assertEqual(work, ['"$B/tasks" work 7', '"$B/tasks" work 8', '"$B/tasks" work 7'])

    def _second_project(self):
        other = self.proj.parent / "other"
        (other / ".agent" / "tasks").mkdir(parents=True)
        return other

    def test_the_same_command_in_two_projects_is_logged_in_both(self):
        # Panel r2 (sonnet, sol-high): the seen-set was global to the shell, so
        # one shell that ran the same text in two projects logged it only in the
        # first. The key now includes the working directory.
        other = self._second_project()
        self._bash('echo same >/dev/null; cd "$O"; echo same >/dev/null', {"O": str(other)})
        self.assertIn("echo same", "\n".join(self._lines()))
        other_hist = other / ".agent" / "bash_history"
        self.assertIn("echo same", other_hist.read_text(encoding="utf-8") if other_hist.exists() else "")

    def test_rotation_is_checked_for_each_project(self):
        # Panel r2 (sonnet, sol-high): the once-per-process rotation flag was set
        # by the FIRST project, so a second project's oversized history was never
        # rotated by that shell.
        other = self._second_project()
        other_hist = other / ".agent" / "bash_history"
        with open(other_hist, "wb") as fh:
            fh.truncate(51 * 1024 * 1024)
        self._bash('echo here >/dev/null; cd "$O"; echo there >/dev/null', {"O": str(other)})
        self.assertTrue([p for p in other_hist.parent.iterdir() if p.name.startswith("bash_history.archived-")])
        self.assertLess(other_hist.stat().st_size, 1024 * 1024)

    def test_rotation_carries_the_task_activations_forward(self):
        # Panel r2 (sol-high, sol-medium): retro builds each task's window from
        # its EARLIEST `tasks work N` line and reads only the live file, so a
        # rotation that archived the active task's activation made its window
        # vanish. The `tasks work|new` lines are copied into the fresh file.
        with open(self.hist, "wb") as fh:
            fh.write(b"2026-09-20 10:00:00 | AGENT | tasks new bugfix x\n"
                     b"2026-09-20 10:00:01 | AGENT | .claude/bin/tasks work 7\n"
                     b"2026-09-20 10:00:02 | AGENT | echo unrelated\n")
            fh.truncate(51 * 1024 * 1024)
        self._bash("echo after-rotation >/dev/null")
        live = self.hist.read_text(encoding="utf-8", errors="replace").splitlines()
        self.assertEqual(live[:2], ["2026-09-20 10:00:00 | AGENT | tasks new bugfix x",
                                    "2026-09-20 10:00:01 | AGENT | .claude/bin/tasks work 7"])
        self.assertTrue(live[2].endswith(" | AGENT | echo after-rotation > /dev/null"), live)
        self.assertEqual(len(live), 3, live)

    def test_rotation_under_errexit_keeps_the_shell_alive(self):
        # Panel r1 P3: the rotation branch (wc/mv/date) ran in no errexit test.
        with open(self.hist, "wb") as fh:
            fh.truncate(51 * 1024 * 1024)
        r = self._bash("set -euo pipefail; echo rotate-errexit >/dev/null; echo still-alive")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("still-alive", r.stdout)
        self.assertTrue([p for p in self.hist.parent.iterdir() if p.name.startswith("bash_history.archived-")])

    def test_a_history_past_50_mb_is_rotated(self):
        with open(self.hist, "wb") as fh:
            fh.truncate(51 * 1024 * 1024)
        self._bash("echo rotate >/dev/null")
        archived = [p.name for p in self.hist.parent.iterdir() if p.name.startswith("bash_history.archived-")]
        self.assertEqual(len(archived), 1, archived)
        # The archive IS the old history (panel r1 P9), not an empty stand-in.
        self.assertEqual((self.hist.parent / archived[0]).stat().st_size, 51 * 1024 * 1024)
        self.assertLess(self.hist.stat().st_size, 1024 * 1024)
        self.assertIn("echo rotate", self.hist.read_text(encoding="utf-8", errors="replace"))

    def test_a_small_history_is_not_rotated(self):
        self.hist.write_text("2026-09-24 00:00:00 | AGENT | echo old\n", encoding="utf-8")
        self._bash("echo new >/dev/null")
        self.assertFalse([p for p in self.hist.parent.iterdir() if p.name.startswith("bash_history.archived-")])
        self.assertIn("echo old", self.hist.read_text(encoding="utf-8"))

    def test_a_set_e_shell_survives_the_trap(self):
        r = self._bash("set -e; [ -d /nonexistent ] || true; false || true; echo still-alive")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("still-alive", r.stdout)


class RotationArchiveIsIgnored(unittest.TestCase):
    """Panel r3 (opus): rotation mints `bash_history.archived-<date>-<pid>`, but the
    ignore block init seeds listed only `bash_history`, so an archive was an
    untracked ~50 MB file in `git status` — and in the close's tree fingerprint."""

    def test_the_seeded_ignore_block_covers_both_archive_names(self):
        import importlib.util
        import shutil
        git = shutil.which("git")
        if not git:
            self.skipTest("git not on PATH")
        merge = Path(__file__).resolve().parent.parent / "plugins" / "playbook" / "scripts" / "claude-md-merge.py"
        spec = importlib.util.spec_from_file_location("claude_md_merge", merge)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run([git, "init", "-q", str(repo)], check=True)
            (repo / ".gitignore").write_bytes(("\n".join(mod.GITIGNORE_ENTRIES) + "\n").encode())
            for rel in (".agent/bash_history.archived-20260924-101010-42",
                        ".agent/alice/bash_history.archived-20260924-101010-42"):
                r = subprocess.run([git, "-C", str(repo), "check-ignore", "-q", rel])
                self.assertEqual(r.returncode, 0, f"{rel} is not ignored")


if __name__ == "__main__":
    unittest.main()
