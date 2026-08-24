#!/usr/bin/env python3
"""Judge isolation + tamper guard (task 018 / bug report #1).

Judges are read-only evaluators. Two independent defenses, both tested here:
  1. OS containment — every judge spawn passes `project_writable=False` to
     `provider.sandbox.run`, so seatbelt/bwrap deny project writes.
  2. Tamper guard — panel & single-judge paths snapshot the repo before spawning
     and hard-stop (non-zero, loud banner, judge.md still saved) if the working
     tree changed, the ONLY defense on uncontained platforms (Windows/nested).

(1) is covered for the five panel adapters (direct call) and the five inline
single-judge cli.py arms (in-process `main()` drive). (2) is covered against the
helpers directly.

Pure stdlib unittest. Run: python3 tests/test_judge_isolation.py
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(_PLUGIN))

from provider.adapters.claude import ClaudeAdapter  # noqa: E402
from provider.adapters.antigravity import AntigravityAdapter  # noqa: E402
from provider.adapters.codex import CodexAdapter  # noqa: E402
from provider.adapters.grok import GrokAdapter  # noqa: E402
from provider.adapters.pi import PiAdapter  # noqa: E402
from tasks import cli as tcli  # noqa: E402
from tasks import review as treview  # noqa: E402  (tamper trio moved by the 1.5.9 split)


def _ok_result(stdout="REVIEW BODY"):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class AdapterContainmentTest(unittest.TestCase):
    """Each panel adapter's run_headless_judge must spawn read-only."""

    ADAPTERS = [
        (ClaudeAdapter, "claude"),
        (AntigravityAdapter, "agy"),
        (CodexAdapter, "codex"),
        (GrokAdapter, "grok"),
        (PiAdapter, "pi"),
    ]

    def _run_and_capture(self, adapter_cls, binary):
        captured = {}

        def fake_run(agent, agent_args, **kwargs):
            captured["agent"] = agent
            captured["kwargs"] = kwargs
            return _ok_result()

        a = adapter_cls(session_id="judge", project_root=Path("/tmp/proj"))
        with mock.patch("shutil.which", return_value=f"/usr/bin/{binary}"), \
             mock.patch("provider.sandbox.run", side_effect=fake_run), \
             mock.patch("provider.sandbox.format_judge_output", side_effect=lambda r: r.stdout):
            a.run_headless_judge(
                prompt="review this", model=None, system_context="CTX",
                web_search=False, timeout_secs=60, budget_usd="10",
            )
        return captured

    def test_all_adapters_pass_project_writable_false(self):
        for adapter_cls, binary in self.ADAPTERS:
            with self.subTest(adapter=binary):
                cap = self._run_and_capture(adapter_cls, binary)
                self.assertIn("kwargs", cap, f"{binary}: sandbox.run was never called")
                self.assertIs(
                    cap["kwargs"].get("project_writable"), False,
                    f"{binary} judge spawned WITHOUT project_writable=False — "
                    "a rogue judge could write the repo",
                )


class InlineSingleJudgeContainmentTest(unittest.TestCase):
    """The five inline single-judge cli.py arms bypass the adapters; each must
    also spawn read-only. Driven through main() so the real dispatch runs."""

    def _drive(self, backend, binary):
        proj = Path(self.tmp)
        captured = {}

        def fake_run(agent, agent_args, **kwargs):
            captured["agent"] = agent
            captured["kwargs"] = kwargs
            return _ok_result()

        argv = ["tasks", "plan-review", "001", "--backend", backend]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch("shutil.which", return_value=f"/usr/bin/{binary}"), \
             mock.patch("provider.sandbox.run", side_effect=fake_run), \
             mock.patch("provider.sandbox.format_judge_output", side_effect=lambda r: r.stdout):
            cwd = os.getcwd()
            os.chdir(proj)
            try:
                tcli.main()
            except SystemExit:
                pass
            finally:
                os.chdir(cwd)
        return captured

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        proj = Path(self.tmp)
        tdir = proj / ".agent" / "tasks" / "001-test"
        tdir.mkdir(parents=True)
        (tdir / "task.md").write_text(
            "# 001 - Test\n\n## Status\npending\n\n## Intent\nx\n\n"
            "## Design Phase\n- [ ] a gate\n", encoding="utf-8")
        (proj / "MIND_MAP.md").write_text("# Mind Map\n[1] node\n", encoding="utf-8")

    def tearDown(self):
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def test_inline_arms_pass_project_writable_false(self):
        for backend, binary in [("claude", "claude"), ("codex", "codex"),
                                ("antigravity", "agy"), ("grok", "grok"),
                                ("pi", "pi")]:
            with self.subTest(backend=backend):
                cap = self._drive(backend, binary)
                self.assertIn("kwargs", cap,
                              f"{backend}: sandbox.run never called (dispatch changed?)")
                self.assertIs(
                    cap["kwargs"].get("project_writable"), False,
                    f"inline {backend} arm spawned WITHOUT project_writable=False",
                )


class TamperGuardTest(unittest.TestCase):
    def _git_repo(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.email", "x@y.z"], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.name", "x"], check=True)
        return d

    def test_clean_run_no_tamper(self):
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        self.assertEqual(treview._detect_tamper(d, tf, before), [])

    def test_monitor_state_churn_is_not_tamper(self):
        # F22 (batch-7 wake 3): the conversation monitor writes trace.md /
        # session.md in .agent/monitor/ WHILE a panel runs — a sanctioned
        # concurrent writer, OS-contained to exactly that dir. Its churn cost
        # the agent a verification cycle when the tamper banner named it.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        mon = d / ".agent" / "monitor"
        mon.mkdir(parents=True)
        (mon / "trace.md").write_text("wake 1\n")
        lane_mon = d / ".agent" / "alice" / "monitor"
        lane_mon.mkdir(parents=True)
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        (mon / "trace.md").write_text("wake 1\nwake 2\n")     # tracked churn
        (mon / "session.md").write_text("judgment\n")          # new file
        (lane_mon / "trace.md").write_text("lane wake\n")      # per-user lane
        self.assertEqual(treview._detect_tamper(d, tf, before), [],
                         "monitor state churn must not read as judge tampering")

    def test_non_monitor_agent_file_still_flags(self):
        # Negative control: the exclusion is the monitor dir ONLY — a new file
        # elsewhere under .agent (or anywhere) still trips the guard.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        (d / ".agent").mkdir()
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        (d / ".agent" / "monitor-notes.md").write_text("rogue\n")
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "a non-monitor .agent file must still flag")

    def test_git_tamper_catches_taskmd_edit_and_new_file(self):
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        tf.write_text("gate1 REWRITTEN BY ROGUE\n")   # task.md rewrite
        (d / "task_audit.md").write_text("fabricated\n")  # new rogue file
        changes = treview._detect_tamper(d, tf, before)
        joined = " ".join(changes)
        self.assertIn("task.md", joined)
        self.assertIn("task_audit.md", joined)

    def test_content_change_to_already_dirty_file_is_caught(self):
        # T3: the dirty-tree gap. A file ALREADY dirty at snapshot time keeps an
        # identical `git status --porcelain` line when only its CONTENT changes,
        # so a rogue judge's content-only edit to it was invisible. Now the
        # snapshot hashes dirty-file contents and the compare flags the change.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        src = d / "src.py"
        src.write_text("x = 1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        src.write_text("x = 2\n")                       # dirty BEFORE the review
        before = treview._snapshot_repo_state(d, tf)
        src.write_text("x = 999  # rogue edit\n")        # same porcelain line
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "content edit to an already-dirty file missed")
        self.assertIn("src.py", " ".join(changes))

    def test_revert_of_already_dirty_file_is_caught(self):
        # T3 panel F1: a rogue that REVERTS an already-dirty tracked file to
        # clean removes its porcelain line and the path vanishes from the
        # content-hash set — caught only by the removed-porcelain-line diff.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        src = d / "src.py"
        src.write_text("x = 1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        src.write_text("x = 2\n")                       # dirty before the review
        before = treview._snapshot_repo_state(d, tf)
        subprocess.run(["git", "-C", str(d), "checkout", "--", "src.py"],
                       check=True)                       # rogue reverts to clean
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "revert-to-clean of a dirty file missed")
        self.assertIn("src.py", " ".join(changes))

    def test_deletion_of_untracked_file_is_caught(self):
        # T3 panel F1: deleting a dirty untracked file also removes its porcelain
        # line — a working-tree mutation the guard must flag.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        junk = d / "wip.txt"
        junk.write_text("draft\n")                       # untracked, dirty
        before = treview._snapshot_repo_state(d, tf)
        junk.unlink()                                    # rogue deletes it
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "deletion of an untracked file missed")
        self.assertIn("wip.txt", " ".join(changes))

    def test_untracked_file_content_change_is_caught(self):
        # T3 panel round 2 (grok F3): the content proof used a TRACKED file; an
        # untracked (`??`) file whose bytes change while staying untracked keeps
        # an identical porcelain line, so only the content hash reveals it.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        u = d / "draft.txt"
        u.write_text("v1\n")                             # untracked, dirty
        before = treview._snapshot_repo_state(d, tf)
        u.write_text("v2 rogue\n")                       # same `?? draft.txt` line
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "untracked content edit missed")
        self.assertIn("draft.txt", " ".join(changes))

    def test_special_char_filename_content_change_is_caught(self):
        # T3 panel round 2 (opus/sonnet/grok F1): a name git C-quotes in the
        # readable porcelain (non-ASCII) OR one containing a literal " -> " must
        # still be hashed — the `-z` enumeration handles both. Line-parsing the
        # readable form dropped these silently.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        tricky = d / "über -> final.txt"                 # non-ASCII AND " -> "
        try:
            tricky.write_text("v1\n")
        except (OSError, UnicodeError):
            # Windows rejects such names (Invalid argument); the guard's own
            # `-z` byte handling is what matters and is exercised on POSIX.
            self.skipTest("OS rejects non-ASCII / ' -> ' filenames")
        before = treview._snapshot_repo_state(d, tf)
        self.assertIn(
            "über -> final.txt", before["dirty_hashes"],
            "special-char filename was not enumerated verbatim for hashing")
        tricky.write_text("v2 rogue\n")
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "special-char filename content edit missed")

    def test_porcelain_z_parser_handles_rename_and_raw_names(self):
        # Unit-level guard on the parser itself (grok F3): a rename record is two
        # NUL fields (dest first, then source — take dest); a raw name with a
        # literal " -> " is one field, not a rename.
        z = b"R  new_name.py\x00old_name.py\x00?? a -> b.txt\x00 M plain.py\x00"
        paths = treview._porcelain_z_paths(z)
        self.assertIn("new_name.py", paths)              # rename destination
        self.assertNotIn("old_name.py", paths)           # source skipped
        self.assertIn("a -> b.txt", paths)               # literal name, not split
        self.assertIn("plain.py", paths)

    def test_taskmd_deletion_in_non_git_repo_is_caught(self):
        # T3 panel round 3 (sonnet, Critical): in the non-git fallback the task
        # hash is the ONLY signal; deleting task.md makes after-hash None, and a
        # bare `b_hash and a_hash` compare short-circuits → the deletion (the
        # PRIMARY tamper target) was invisible.
        import tempfile
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text("gate1\n")
        before = treview._snapshot_repo_state(d, tf)
        self.assertIsNone(before["porcelain"])           # not a git repo
        tf.unlink()                                      # rogue deletes task.md
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "task.md deletion in non-git fallback missed")
        self.assertIn("deleted", " ".join(changes))

    def test_real_git_mv_rename_hashes_destination(self):
        # T3 panel round 3 (opus F2): confirm against REAL git that `-z` emits a
        # rename as <dest>\0<source>, so the parser hashes the file that exists.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        (d / "orig.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        subprocess.run(["git", "-C", str(d), "mv", "orig.py", "renamed.py"],
                       check=True)
        before = treview._snapshot_repo_state(d, tf)
        self.assertIn("renamed.py", before["dirty_hashes"],
                      "rename destination not enumerated for hashing")
        self.assertNotIn("orig.py", before["dirty_hashes"],
                         "rename source hashed by mistake (field order wrong)")

    def test_non_utf8_or_cr_filename_content_change_is_caught(self):
        # T3 panel round 3 (grok): a name with `\r` or invalid UTF-8 must still
        # be enumerated and hashed — the `-z` bytes read + os.fsdecode handles
        # it, where a text/errors=replace decode silently dropped it.
        import os
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        raw = os.fsencode(str(d)) + b"/weird\x80\rname.txt"
        try:
            with open(raw, "wb") as fh:
                fh.write(b"v1\n")
        except (OSError, ValueError):
            self.skipTest("OS rejects non-UTF-8 / CR filenames")
        before = treview._snapshot_repo_state(d, tf)
        self.assertTrue(before["dirty_hashes"],
                        "weird-name dirty file was not enumerated for hashing")
        with open(raw, "wb") as fh:
            fh.write(b"v2 rogue\n")                       # same porcelain line
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "content edit to a weird-name file missed")

    def test_symlink_retarget_same_content_is_caught(self):
        # T3 panel round 4 (grok): a dirty symlink retargeted to a different file
        # with IDENTICAL content keeps ` M link` and the same referent bytes, so
        # hashing the referent missed it — the link-text hash catches it.
        import os
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        (d / "orig.txt").write_text("committed\n")
        (d / "t1.txt").write_text("same\n")
        (d / "t2.txt").write_text("same\n")          # identical content to t1
        try:
            os.symlink("orig.txt", str(d / "link"))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks unsupported on this platform")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        os.remove(d / "link"); os.symlink("t1.txt", str(d / "link"))  # dirty
        before = treview._snapshot_repo_state(d, tf)
        os.remove(d / "link"); os.symlink("t2.txt", str(d / "link"))  # rogue retarget
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "symlink retarget (same referent content) missed")
        self.assertIn("link", " ".join(changes))

    def test_untouched_dirty_symlink_is_not_flagged(self):
        # T3 panel round 5 (sonnet #2): the negative control for the symlink
        # proof — a dirty symlink left UNCHANGED during the review must not
        # false-positive (same mechanism as the symlink-retarget proof).
        import os
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        (d / "orig.txt").write_text("committed\n")
        (d / "t1.txt").write_text("target\n")
        try:
            os.symlink("orig.txt", str(d / "link"))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks unsupported on this platform")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        os.remove(d / "link"); os.symlink("t1.txt", str(d / "link"))  # dirty
        before = treview._snapshot_repo_state(d, tf)
        # ...and left untouched during the review
        self.assertEqual(treview._detect_tamper(d, tf, before), [],
                         "an untouched dirty symlink must not read as tamper")

    def test_degraded_guard_when_z_enumeration_failed_is_flagged(self):
        # T3 panel round 6 (opus/sonnet/grok, unanimous): the round-5 fail-CLOSED
        # path must be watched. A git snapshot whose `-z` read failed has a
        # hollow `dirty_hashes`, so the content-hash compare would silently pass
        # — it must instead surface a loud degraded-guard change. Injecting a
        # before-snapshot with `z_read_ok=False` needs no real git failure.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = {"porcelain": "", "task_hash": None, "dirty_hashes": {},
                  "z_read_ok": False}                  # git repo, -z enumeration failed
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(any("degraded" in c for c in changes),
                        "a failed -z enumeration must surface a degraded-guard "
                        "warning, not a silent pass")

    def test_git_directory_deletion_is_caught(self):
        # T3 panel round 4 (sonnet, Critical): deleting .git makes `git status`
        # fail after but not before — a git↔non-git transition that must flag,
        # even when task.md is untouched.
        import shutil
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        self.assertIsNotNone(before["porcelain"])
        # Windows/git-bash marks pack files read-only and may hold handles, so a
        # plain rmtree of .git raises WinError 5 — clear the read-only bit and
        # retry; if the OS still refuses, skip rather than error (the transition
        # logic is proven on POSIX and unit-injectable elsewhere).
        import os as _os
        import stat as _stat
        def _force(_func, _path, _exc):
            try:
                _os.chmod(_path, _stat.S_IWRITE)
                _func(_path)
            except OSError:
                pass
        try:
            shutil.rmtree(d / ".git", onerror=_force)   # rogue destroys the repo
        except OSError:
            self.skipTest("OS will not let the test delete .git")
        if (d / ".git").exists():
            self.skipTest("OS retained .git despite rmtree (locked handles)")
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, ".git deletion produced no tamper signal")
        self.assertIn("unreadable", " ".join(changes))

    def test_total_byte_budget_marks_excess_files(self):
        # T3 panel round 4 (opus): the cumulative-bytes ceiling marks files past
        # the budget with an honest marker instead of hashing an unbounded tree.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        for i in range(4):
            (d / f"f{i}.txt").write_text("x" * 1000 + f"{i}\n")
        orig = treview._TAMPER_TOTAL_BUDGET
        treview._TAMPER_TOTAL_BUDGET = 1500          # ~1.5 files' worth
        try:
            snap = treview._snapshot_repo_state(d, tf)
        finally:
            treview._TAMPER_TOTAL_BUDGET = orig
        marks = [v for v in snap["dirty_hashes"].values()
                 if v == "unhashed:budget-exceeded"]
        self.assertTrue(marks, "budget ceiling never marked any file")

    def test_untouched_dirty_file_is_not_flagged(self):
        # Negative control: an already-dirty file left ALONE during the review
        # must not false-positive (else every real task, whose own edits are
        # dirty at review time, would trip the guard).
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        src = d / "src.py"
        src.write_text("x = 1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        src.write_text("x = 2\n")                       # dirty, then untouched
        (d / "untracked.txt").write_text("note\n")       # dirty untracked, untouched
        before = treview._snapshot_repo_state(d, tf)
        self.assertEqual(treview._detect_tamper(d, tf, before), [],
                         "an untouched dirty file must not read as tamper")

    def test_monitor_dirty_content_churn_is_not_tamper(self):
        # The monitor writes under .agent[/lane]/monitor/ WHILE panels run; a
        # content change to a monitor file that was already dirty must stay
        # excluded, same as the porcelain-line exclusion.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        mon = d / ".agent" / "monitor"
        mon.mkdir(parents=True)
        (mon / "trace.md").write_text("wake 1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        (mon / "trace.md").write_text("wake 1\nwake 2\n")   # dirty before review
        before = treview._snapshot_repo_state(d, tf)
        (mon / "trace.md").write_text("wake 1\nwake 2\nwake 3\n")  # churns again
        self.assertEqual(treview._detect_tamper(d, tf, before), [],
                         "monitor content churn must stay excluded")

    def test_oversize_dirty_file_uses_honest_marker(self):
        # The content hash is size-capped; an oversize dirty file records an
        # honest "unhashed" marker rather than being silently skipped, and a
        # same-size oversize file does not false-positive.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        big = d / "big.bin"
        big.write_bytes(b"a" * (treview._TAMPER_HASH_CAP + 10))
        before = treview._snapshot_repo_state(d, tf)
        self.assertIn("big.bin", before["dirty_hashes"])
        self.assertIn("unhashed", before["dirty_hashes"]["big.bin"])
        # untouched oversize → no flag
        self.assertEqual(treview._detect_tamper(d, tf, before), [])

    def test_non_git_repo_falls_back_to_taskmd_hash(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text("a\n")
        before = treview._snapshot_repo_state(d, tf)
        self.assertIsNone(before["porcelain"])          # not a git repo
        self.assertIsNotNone(before["task_hash"])
        self.assertEqual(treview._detect_tamper(d, tf, before), [])   # unchanged
        tf.write_text("b\n")
        self.assertTrue(treview._detect_tamper(d, tf, before))        # changed

    def test_banner_names_changes_and_says_do_not_ingest(self):
        banner = treview._tamper_banner(["working tree: ?? rogue.md",
                                      "task.md content changed (task.md)"])
        self.assertIn("TAMPER DETECTED", banner)
        self.assertIn("rogue.md", banner)
        self.assertIn("Do NOT ingest", banner)

    def test_no_taskmd_and_non_git_yields_no_signal(self):
        # Promptless panel on a non-git dir with no task.md: nothing to compare,
        # detector must return [] (no false positive), not crash.
        import tempfile
        d = Path(tempfile.mkdtemp())
        before = treview._snapshot_repo_state(d, None)
        self.assertEqual(treview._detect_tamper(d, None, before), [])

    def test_taskmd_replaced_by_fifo_is_flagged_not_read(self):
        # panel round-3 (codex:sol Critical): a rogue that swaps task.md for a
        # FIFO (or a symlink to a blocking device) would make an `exists()` +
        # `read_bytes()` fingerprint BLOCK FOREVER before the exit banner. lstat
        # now rejects non-regular files without reading them — flagged, no hang.
        # task.md is gitignored so the ONLY signal is the task fingerprint.
        if not hasattr(os, "mkfifo") or not hasattr(__import__("signal"), "SIGALRM"):
            self.skipTest("os.mkfifo / SIGALRM unavailable (Windows)")
        import signal
        d = self._git_repo()
        (d / ".gitignore").write_text("task.md\n")
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        tf.unlink()
        os.mkfifo(str(tf))                      # task.md is now a FIFO

        # Fail FAST rather than hang the whole suite if the guard regresses to a
        # blocking read (codex:sol round-4 Important): a SIGALRM watchdog turns a
        # hang into a prompt failure instead of the 900s verifier timeout.
        def _boom(_signum, _frame):
            raise TimeoutError("tamper detector blocked reading a FIFO task.md")
        old = signal.signal(signal.SIGALRM, _boom)
        signal.alarm(15)
        try:
            changes = treview._detect_tamper(d, tf, before)   # must NOT hang
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        self.assertTrue(changes, "task.md replaced by a FIFO produced no signal")
        self.assertIn("regular file", " ".join(changes))

    def test_taskmd_replaced_by_symlink_to_same_content_is_caught(self):
        # panel round-3 (codex:terra): a rogue that replaces an ignored task.md
        # with a SYMLINK to a same-content decoy read identical under the old
        # dereferencing fingerprint (evasion). lstat now fingerprints the LINK
        # TEXT, so the swap is caught even when git status is silent (ignored).
        d = self._git_repo()
        (d / ".gitignore").write_text("task.md\n")
        tf = d / "task.md"
        tf.write_text("gate1\n")
        (d / "decoy.txt").write_text("gate1\n")           # identical content
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        tf.unlink()
        try:
            os.symlink("decoy.txt", str(tf))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks unsupported on this platform")
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes,
                        "symlink-to-same-content task.md swap slipped the fingerprint")

    def test_regular_taskmd_edit_still_hashes_by_content(self):
        # Negative/positive control for the lstat fingerprint: an ORDINARY
        # regular-file task.md edit is still hashed by content (caught), and an
        # untouched regular task.md does not false-positive.
        d = self._git_repo()
        (d / ".gitignore").write_text("task.md\n")
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        self.assertEqual(treview._detect_tamper(d, tf, before), [])   # untouched
        tf.write_text("gate1 rewritten\n")
        self.assertTrue(treview._detect_tamper(d, tf, before))        # content edit

    def test_taskmd_grown_past_cap_is_bounded_and_flagged(self):
        # panel round-4 (codex:terra Critical): a rogue that grows task.md to
        # gigabytes then times out must not make the fingerprint load it all
        # (MemoryError/stall before the banner). The read is size-capped: an
        # oversize task.md records a size-bearing "too-large:<size>" marker and
        # the grow is flagged — without reading past the cap.
        d = self._git_repo()
        (d / ".gitignore").write_text("task.md\n")
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        self.assertNotEqual(before["task_hash"], "unreadable")     # small = hashed
        tf.write_bytes(b"a" * (treview._TAMPER_HASH_CAP + 1024))   # grown past cap
        after = treview._snapshot_repo_state(d, tf)
        self.assertTrue(after["task_hash"].startswith("too-large:"))  # size-bearing, not loaded
        self.assertTrue(treview._detect_tamper(d, tf, before))     # flagged

    def test_unchanged_oversize_taskmd_is_not_flagged(self):
        # panel round-6 (opus/codex:sol): a task.md that legitimately exceeds the
        # 5 MiB cap must NOT trip the tamper banner on every review — the round-5
        # unconditional fail-closed did. The size-bearing `too-large:<size>`
        # marker is identical across an UNCHANGED oversize file, so no flag.
        d = self._git_repo()
        (d / ".gitignore").write_text("task.md\n")
        tf = d / "task.md"
        tf.write_bytes(b"a" * (treview._TAMPER_HASH_CAP + 4096))    # oversize, untouched
        subprocess.run(["git", "-C", str(d), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        self.assertEqual(treview._detect_tamper(d, tf, before), [],
                         "an unchanged oversize task.md must not false-positive")

    def test_tracked_dirty_file_swapped_to_fifo_is_flagged(self):
        # panel round-8 (codex:terra): a tracked, already-dirty file (` M victim`)
        # swapped to a FIFO — whose porcelain line can stay unchanged on some git
        # versions — must still be flagged. The dirty-file loop now records a
        # stable `nonregular:<type>` marker, so regular-hash → nonregular compares
        # unequal instead of the FIFO vanishing from the hash set.
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo unavailable (Windows)")
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        victim = d / "victim.txt"
        victim.write_text("committed\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        victim.write_text("dirty\n")                       # already dirty at snapshot
        before = treview._snapshot_repo_state(d, tf)
        self.assertIn("victim.txt", before["dirty_hashes"])   # hashed as a regular file
        victim.unlink()
        os.mkfifo(str(victim))                             # rogue swaps it to a FIFO
        after = treview._snapshot_repo_state(d, tf)
        self.assertTrue(after["dirty_hashes"].get("victim.txt", "").startswith("nonregular:"))
        self.assertTrue(treview._detect_tamper(d, tf, before),
                        "a tracked dirty file swapped to a FIFO was missed")

    def test_safe_hash_regular_does_not_block_on_fifo(self):
        # panel round-6 (sonnet Critical / opus): the shared safe-read primitive
        # must never block on a non-regular file (a FIFO with no writer would
        # hang a plain read forever). Directly exercises the TOCTOU protection
        # (fstat rejects a FIFO opened under O_NONBLOCK).
        if not hasattr(os, "mkfifo") or not hasattr(__import__("signal"), "SIGALRM"):
            self.skipTest("os.mkfifo / SIGALRM unavailable (Windows)")
        import signal
        d = self._git_repo()
        fifo = d / "pipe"
        os.mkfifo(str(fifo))

        def _boom(_s, _f):
            raise TimeoutError("safe reader blocked on a FIFO")
        old = signal.signal(signal.SIGALRM, _boom)
        signal.alarm(15)
        try:
            kind, _detail, _n = treview._safe_hash_regular(fifo, treview._TAMPER_HASH_CAP)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        self.assertEqual(kind, "error")   # a FIFO is never a regular file to hash

    def test_dirty_file_replaced_by_fifo_does_not_hang(self):
        # panel round-6 (sonnet Critical): the dirty-file content-hash loop routes
        # through the same safe reader, so a dirty file that is a FIFO does not
        # block _snapshot_repo_state — the whole review can't be wedged.
        if not hasattr(os, "mkfifo") or not hasattr(__import__("signal"), "SIGALRM"):
            self.skipTest("os.mkfifo / SIGALRM unavailable (Windows)")
        import signal
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "task.md"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        os.mkfifo(str(d / "pipe.dat"))          # an untracked dirty FIFO

        def _boom(_s, _f):
            raise TimeoutError("dirty-file loop blocked on a FIFO")
        old = signal.signal(signal.SIGALRM, _boom)
        signal.alarm(15)
        try:
            snap = treview._snapshot_repo_state(d, tf)   # must NOT hang
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        self.assertNotIn("pipe.dat", snap["dirty_hashes"])   # skipped, never read

    def test_oversize_taskmd_content_rewrite_is_caught(self):
        # panel round-5 (codex:sol) / round-6 fix: an oversize task.md is
        # fingerprinted by a SIZE-BEARING `too-large:<size>` marker, so an oversize
        # rewrite that changes the SIZE is caught by the ordinary before/after
        # compare (no fail-closed needed, and no false-positive on an unchanged
        # oversize file — see test_unchanged_oversize_taskmd_is_not_flagged). A
        # same-SIZE, different-content oversize rewrite is the documented cost of
        # not loading gigabytes (ledger limitation), so this exercises a size
        # change. task.md is gitignored so only the fingerprint is the signal.
        d = self._git_repo()
        (d / ".gitignore").write_text("task.md\n")
        tf = d / "task.md"
        tf.write_bytes(b"a" * (treview._TAMPER_HASH_CAP + 1024))    # oversize baseline
        subprocess.run(["git", "-C", str(d), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        self.assertTrue(before["task_hash"].startswith("too-large:"))
        tf.write_bytes(b"b" * (treview._TAMPER_HASH_CAP + 2048))    # different oversize SIZE
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes,
                        "oversize->oversize task.md rewrite (size change) slipped the compare")


class SingleJudgeExitTamperTest(unittest.TestCase):
    """A1: every post-snapshot single-judge exit (timeout / budget / dead-pin)
    must surface the tamper banner and exit nonzero, matching the panel path. A
    judge that mutates the working tree and then bails via one of these exits
    must not escape the loud stop. Driven through `main()` so the real dispatch
    in `cmd_single_review` runs; a faked `provider.sandbox.run` stands in for the
    spawned judge and performs the mutation before the exit."""

    def _project(self):
        import shutil as _sh
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: _sh.rmtree(d, ignore_errors=True))
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.email", "x@y.z"], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.name", "x"], check=True)
        tdir = d / ".agent" / "tasks" / "001-test"
        tdir.mkdir(parents=True)
        (tdir / "task.md").write_text(
            "# 001 - Test\n\n## Status\npending\n\n## Intent\nx\n\n"
            "## Plan Review\n(plan review triage appears here)\n\n"
            "## Design Phase\n- [ ] a gate\n", encoding="utf-8")
        (d / "MIND_MAP.md").write_text("# Mind Map\n[1] node\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        return d

    def _drive(self, proj, fake_run, extra_argv=(), patches=(), real_format=False):
        import contextlib
        import io
        argv = ["tasks", "plan-review", "001", "--backend", "claude", *extra_argv]
        err, out = io.StringIO(), io.StringIO()
        code = None
        cwd = os.getcwd()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch("shutil.which", return_value="/usr/bin/claude"))
            stack.enter_context(mock.patch("provider.sandbox.run", side_effect=fake_run))
            if not real_format:
                # Default: stdout is the review text. The dead-pin tests set
                # real_format=True so the genuine `(FAILED — exit N)` wrapper
                # drives classify_failure / confirm_dead_specs for real.
                stack.enter_context(mock.patch("provider.sandbox.format_judge_output",
                                               side_effect=lambda r: (r.stdout or "")))
            for p in patches:
                stack.enter_context(p)
            os.chdir(proj)
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                    tcli.main()
            except SystemExit as e:
                code = e.code
            finally:
                os.chdir(cwd)
        return code, err.getvalue(), out.getvalue()

    @staticmethod
    def _rogue(kw):
        (Path(kw["project_root"]) / "rogue.md").write_text("fabricated\n")

    def test_timeout_exit_after_tamper_is_caught(self):
        proj = self._project()

        def fake(agent, agent_args, **kw):
            self._rogue(kw)
            raise subprocess.TimeoutExpired(cmd="claude", timeout=60, output="partial")
        code, err, _ = self._drive(proj, fake, extra_argv=["--timeout", "60"])
        self.assertEqual(code, 1)
        self.assertIn("TAMPER DETECTED", err,
                      "a judge that tampered then timed out escaped detection")
        self.assertIn("rogue.md", err)

    def test_clean_timeout_prints_no_tamper_banner(self):
        # Negative control: a clean timeout keeps its normal message and prints
        # NO banner — and, because `_bail_review_timeout` writes a partial log
        # under the task dir, this also proves tamper is computed BEFORE that
        # write (else the partial log would self-flag).
        proj = self._project()

        def fake(agent, agent_args, **kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=60, output="partial")
        code, err, _ = self._drive(proj, fake, extra_argv=["--timeout", "60"])
        self.assertEqual(code, 1)
        self.assertNotIn("TAMPER DETECTED", err)
        self.assertIn("hard timeout", err)

    def test_clean_dead_pin_survives_raising_diagnostics(self):
        # codex:sol round-7/9: on a CLEAN dead-pin run (no tamper), a raise inside
        # the availability report (check_pins/render_report) must not crash the
        # run — the report is best-effort. (Tamper protection itself is now
        # structural: the early hard-stop emits the banner and exits before the
        # dead-pin flow ever runs — see test_dead_pin_exit_after_tamper_is_caught.)
        proj = self._project()
        from tasks import models_check as mc
        patches = [
            mock.patch("tasks.models_check.probe_claude_model",
                       return_value=(mc.GONE, "model gone")),
            mock.patch("tasks.models_check.check_pins",
                       side_effect=RuntimeError("malformed provider cache")),
            mock.patch("tasks.models_check.render_report", return_value=""),
        ]

        def fake(agent, agent_args, **kw):
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout=self._BADMODEL, stderr="")
        code, err, _ = self._drive(
            proj, fake, extra_argv=["--model", "badmodel"],
            patches=patches, real_format=True)
        self.assertEqual(code, 1)
        self.assertIn("HARD STOP", err, "the dead-pin flow did not fire")
        self.assertIn("availability report unavailable", err,
                      "a raising availability report was not handled best-effort")

    def test_timeout_after_taskdir_deletion_still_emits_banner(self):
        # codex:sol Critical (panel round 1): a rogue that deletes the task dir
        # then times out would make the partial-log write raise BEFORE the
        # banner, swallowing the tamper signal. The banner is now emitted right
        # after detection and the partial-log write is failure-tolerant, so the
        # signal survives a hostile tree and the exit code is still reached.
        import shutil as _sh
        proj = self._project()
        tdir = proj / ".agent" / "tasks" / "001-test"

        def fake(agent, agent_args, **kw):
            _sh.rmtree(tdir)   # rogue destroys the task dir, then hangs
            raise subprocess.TimeoutExpired(cmd="claude", timeout=60, output="partial")
        code, err, _ = self._drive(proj, fake, extra_argv=["--timeout", "60"])
        self.assertEqual(code, 1)
        self.assertIn("TAMPER DETECTED", err,
                      "banner lost when the partial-log write raised on a deleted task dir")

    def test_success_path_taskdir_deletion_still_emits_banner(self):
        # codex:terra round-5 Critical: a judge that deletes the task dir and
        # returns 0 must still reach the tamper banner — the intervening
        # judge_log save (now best-effort) must not crash first.
        import shutil as _sh
        proj = self._project()
        tdir = proj / ".agent" / "tasks" / "001-test"

        def fake(agent, agent_args, **kw):
            _sh.rmtree(tdir)     # rogue destroys the task dir, then returns clean
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="REVIEW BODY", stderr="")
        code, err, _ = self._drive(proj, fake)
        self.assertEqual(code, 1)
        self.assertIn("TAMPER DETECTED", err,
                      "success-path judge_log save crashed before the banner")

    def test_clean_review_refuses_findings_when_log_save_fails(self):
        # codex:sol round-6: the best-effort judge_log save must not let a CLEAN
        # review ingest findings with no durable log — if the save fails, refuse
        # the write-back and exit nonzero (chain of custody).
        proj = self._project()

        def fake(agent, agent_args, **kw):
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="REVIEW BODY", stderr="")
        patches = [mock.patch("tasks.review.atomic_write",
                              side_effect=OSError("disk full"))]
        code, err, _ = self._drive(proj, fake, patches=patches)
        self.assertEqual(code, 1)
        self.assertIn("chain of custody", err)

    def test_budget_exit_after_tamper_is_caught(self):
        proj = self._project()

        def fake(agent, agent_args, **kw):
            self._rogue(kw)
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="Error: Exceeded USD budget of $10\n", stderr="")
        code, err, _ = self._drive(proj, fake)
        self.assertEqual(code, 1)
        self.assertIn("TAMPER DETECTED", err,
                      "a judge that tampered then blew budget escaped detection")

    def test_clean_budget_exit_prints_no_tamper_banner(self):
        proj = self._project()

        def fake(agent, agent_args, **kw):
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="Error: Exceeded USD budget of $10\n", stderr="")
        code, err, out = self._drive(proj, fake)
        self.assertEqual(code, 1)
        self.assertNotIn("TAMPER DETECTED", err)
        self.assertIn("budget cap", out)

    def _dead_pin_patches(self):
        # Faithful dead-pin flow (codex:sol Important): mock ONLY the live model
        # probe, so the real format_judge_output → classify_failure →
        # confirm_dead_specs chain runs against a genuine `(FAILED — exit 1)`
        # bad-model output. check_pins/apply_confirmed/render_report only render
        # the post-confirmation availability report to stderr — stubbed so the
        # test needs no models.json.
        from tasks import models_check as mc
        return [
            mock.patch("tasks.models_check.probe_claude_model",
                       return_value=(mc.GONE, "model gone")),
            mock.patch("tasks.models_check.check_pins", return_value={}),
            mock.patch("tasks.models_check.apply_confirmed", return_value={}),
            mock.patch("tasks.models_check.render_report", return_value=""),
        ]

    # claude prints its bad-model error to stdout with exit 1; format_judge_output
    # wraps it `(FAILED — exit 1)` and classify_failure keys on this exact anchor.
    _BADMODEL = "There's an issue with the selected model"

    def test_dead_pin_exit_after_tamper_is_caught(self):
        proj = self._project()

        def fake(agent, agent_args, **kw):
            self._rogue(kw)
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout=self._BADMODEL, stderr="")
        code, err, _ = self._drive(
            proj, fake, extra_argv=["--model", "badmodel"],
            patches=self._dead_pin_patches(), real_format=True)
        self.assertEqual(code, 1)
        # The early tamper hard-stop now fires BEFORE the dead-pin flow (the
        # structural round-8 fix), so tamper on a dead-pin-shaped run is caught
        # up front — "HARD STOP" need not appear; the banner is what matters.
        self.assertIn("TAMPER DETECTED", err,
                      "a judge that tampered then hit a dead pin escaped detection")

    def test_clean_dead_pin_exit_prints_no_tamper_banner(self):
        proj = self._project()

        def fake(agent, agent_args, **kw):
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout=self._BADMODEL, stderr="")
        code, err, _ = self._drive(
            proj, fake, extra_argv=["--model", "badmodel"],
            patches=self._dead_pin_patches(), real_format=True)
        self.assertEqual(code, 1)
        self.assertNotIn("TAMPER DETECTED", err)
        self.assertIn("HARD STOP", err)

    def test_timeout_after_taskmd_replaced_by_dir_still_emits_banner(self):
        # panel round-2 (codex:sol + codex:terra, convergent Critical):
        # `_snapshot_repo_state` hashed task.md with an unguarded read_bytes(), so
        # a rogue that replaces task.md with a DIRECTORY then times out raised
        # IsADirectoryError inside `_detect_tamper` — crashing the exit before its
        # banner. The read is now guarded (an "unreadable" sentinel), so the
        # tamper still surfaces instead of an uncaught crash.
        proj = self._project()
        tf = proj / ".agent" / "tasks" / "001-test" / "task.md"

        def fake(agent, agent_args, **kw):
            tf.unlink()
            tf.mkdir()   # task.md is now a directory → read_bytes would raise
            raise subprocess.TimeoutExpired(cmd="claude", timeout=60, output="partial")
        code, err, _ = self._drive(proj, fake, extra_argv=["--timeout", "60"])
        self.assertEqual(code, 1)
        self.assertIn("TAMPER DETECTED", err,
                      "task.md replaced by a directory crashed the guard before the banner")
        self.assertIn("no longer a readable regular file", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
