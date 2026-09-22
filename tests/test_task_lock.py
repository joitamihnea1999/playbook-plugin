#!/usr/bin/env python3
"""The per-task transaction lock and the read-transform-write primitive (task 058).

Every task.md writer is atomic per WRITE (`tasks.atomic`) but not per
TRANSACTION. Measured baseline for this task: the real close shape — read
task.md, run the verify contract for minutes, write the receipt — lost a
concurrent `tasks blocked` in **5 of 5** trials, and the `## Blocked` record
vanished entirely while the task stayed `in_progress`. These tests pin the lock,
the primitive, and that regression.

Pure stdlib unittest; the multi-process cases use real subprocesses.
Run: python3 -m unittest tests.test_task_lock
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

from tasks import filelock  # noqa: E402
from tasks.atomic import rewrite  # noqa: E402


def _tmp() -> Path:
    import shutil
    d = Path(tempfile.mkdtemp())
    unittest.TestCase.addTypeEqualityFunc  # noqa: B018 (keep the import honest)
    return d


class Backend(unittest.TestCase):
    def test_a_backend_is_selected_and_named(self):
        b = filelock.selected_backend()
        self.assertIn(b, ("fcntl", "msvcrt", "none"), b)

    def test_this_platform_uses_the_expected_backend(self):
        # 058 plan panel (opus #3): "the Windows lane is the msvcrt path" was an
        # ASSUMPTION. Assert it, so a lane that silently took another branch
        # fails loudly instead of reporting a green proof of an untested one.
        b = filelock.selected_backend()
        if sys.platform.startswith("win"):
            self.assertEqual(b, "msvcrt", "the windows lane did not exercise msvcrt")
        else:
            self.assertEqual(b, "fcntl", f"{sys.platform} did not exercise fcntl")


class LockBasics(unittest.TestCase):
    def setUp(self):
        self.d = _tmp()
        self.f = self.d / "task.md"
        self.f.write_text("x\n", encoding="utf-8")

    def test_acquire_and_release(self):
        with filelock.task_lock(self.f):
            pass
        with filelock.task_lock(self.f):
            pass                                   # a second acquire must succeed

    def test_lock_file_is_persistent(self):
        # 058 plan panel (codex ×2, Critical): unlinking on release lets a waiter
        # keep the old inode while a third process locks a new one — two owners.
        with filelock.task_lock(self.f):
            lock_path = filelock.lock_path_for(self.f)
            self.assertTrue(lock_path.exists())
        self.assertTrue(lock_path.exists(), "the lock file must survive release")

    def test_nesting_in_one_process_does_not_deadlock(self):
        # 058 plan panel (grok F5): a second flock on a NEW fd deadlocks on
        # BSD/macOS and a second msvcrt.locking on the same range fails, so the
        # holder is refcounted and locks only on the 0→1 transition.
        with filelock.task_lock(self.f):
            with filelock.task_lock(self.f):
                with filelock.task_lock(self.f):
                    pass
        self.assertEqual(filelock._depth_for(self.f), 0)

    def test_released_on_systemexit(self):
        with self.assertRaises(SystemExit):
            with filelock.task_lock(self.f):
                raise SystemExit(1)
        self.assertEqual(filelock._depth_for(self.f), 0)
        with filelock.task_lock(self.f):
            pass

    def test_timeout_names_the_holder(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(PLUGIN)!r})
                from tasks import filelock
                with filelock.task_lock({str(self.f)!r}):
                    print("HELD", flush=True)
                    time.sleep(8)
            """)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "HELD")
            if filelock.selected_backend() == "none":
                self.skipTest("no locking backend on this platform")
            t0 = time.monotonic()
            with self.assertRaises(filelock.LockTimeout) as cm:
                with filelock.task_lock(self.f, timeout=1.0):
                    pass
            self.assertLess(time.monotonic() - t0, 6.0, "the wait was not bounded")
            self.assertIn(str(holder.pid), str(cm.exception),
                          "the timeout must name the holder pid")
        finally:
            holder.kill()
            holder.wait()

    def test_degrades_loudly_when_no_backend_exists(self):
        import io
        import contextlib
        err = io.StringIO()
        with mock.patch.object(filelock, "_BACKEND", "none"), \
             mock.patch.object(filelock, "_ADVISED", False):
            with contextlib.redirect_stderr(err):
                with filelock.task_lock(self.f):
                    self.f.write_text("written anyway\n", encoding="utf-8")
        self.assertEqual(self.f.read_text(encoding="utf-8"), "written anyway\n")
        self.assertIn("without locking", err.getvalue().lower())

    def test_os_releases_the_lock_when_the_holder_dies(self):
        # 058 plan panel (opus #4, sonnet #4): this is WHY there is no stale-lock
        # stealing — the OS does it, and a pid-based steal would let two writers
        # proceed, reintroducing the lost update.
        if filelock.selected_backend() == "none":
            self.skipTest("no locking backend on this platform")
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(PLUGIN)!r})
                from tasks import filelock
                with filelock.task_lock({str(self.f)!r}):
                    print("HELD", flush=True)
                    time.sleep(30)
            """)], stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "HELD")
        holder.kill()
        holder.wait()
        with filelock.task_lock(self.f, timeout=5.0):
            pass                                   # must not raise


class RewritePrimitive(unittest.TestCase):
    def setUp(self):
        self.d = _tmp()
        self.f = self.d / "task.md"
        self.f.write_text("one\n", encoding="utf-8")

    def test_applies_the_transform(self):
        out = rewrite(self.f, lambda t: t + "two\n")
        self.assertEqual(out, "one\ntwo\n")
        self.assertEqual(self.f.read_text(encoding="utf-8"), "one\ntwo\n")

    def test_transform_returning_none_declines_the_write(self):
        # The `_set_status`-returns-False shape: "nothing to write" is not an error.
        out = rewrite(self.f, lambda t: None)
        self.assertIsNone(out)
        self.assertEqual(self.f.read_text(encoding="utf-8"), "one\n")

    def test_raising_transform_leaves_the_file_byte_identical(self):
        before = self.f.read_bytes()
        with self.assertRaises(ValueError):
            rewrite(self.f, lambda t: (_ for _ in ()).throw(ValueError("no")))
        self.assertEqual(self.f.read_bytes(), before)
        self.assertEqual(filelock._depth_for(self.f), 0, "the lock leaked")

    def test_composes_under_an_outer_lock(self):
        with filelock.task_lock(self.f):
            rewrite(self.f, lambda t: t + "a\n")
            rewrite(self.f, lambda t: t + "b\n")
        self.assertEqual(self.f.read_text(encoding="utf-8"), "one\na\nb\n")


class NoUnapprovedDirectWriters(unittest.TestCase):
    """058 plan panel (codex ×2, Critical): the routing inventory must not rot.
    A NEW direct writer of task.md / judge.md that skips the transaction is the
    exact way this guarantee decays, so the package is grepped and any call site
    outside the approved set fails this test."""

    # (filename, substring of the call line) -> why a direct write is correct there.
    # Everything else must route through `atomic.rewrite` (or hold `task_lock`
    # across its own multi-file protocol).
    APPROVED = {
        ("core.py", "_atomic_write(task_file, content)"):
            "task CREATION writes a brand-new file in a directory just made — "
            "there is no prior content to lose and no concurrent writer",
        ("core.py", "_atomic_write(archive_path, header"):
            "inside stack_judge_round's two-file protocol, which holds the task "
            "lock across the whole sequence (its rollback is the crash contract)",
        ("core.py", "_atomic_write(judge_md, out)"):
            "same two-file protocol, under the same held lock",
        ("core.py", "atomic_write(archive_path, archive_backup)"):
            "the rollback arm of that protocol, under the same held lock",
        ("history.py", "atomic_write(task_file, retro_content)"):
            "retro CREATES a new task file in a directory it just made",
        ("lifecycle.py", "_atomic_write("):
            "the freehand task CREATION writes a brand-new file (checked by eye: "
            "the only remaining _atomic_write in lifecycle is that creation)",
        ("review.py", "atomic_write(task_file, new_text)"):
            "inside _write_review_findings_locked, whose caller holds the task "
            "lock for the whole read-splice-write (it keeps a soft error-string "
            "contract, so it is not expressed as a rewrite transform)",
        ("compact.py", "_atomic_write(task_md, new_task_text)"):
            "inside tasks compact's two-file move, which holds the task lock "
            "across the archive append and the task.md replace (its truncate-back "
            "rollback is the crash contract)",
        ("core.py", "def _atomic_write"): "the helper's own definition",
        ("core.py", "atomic_write(path, text)"): "inside that helper",
        ("compact.py", "def _atomic_write"): "the helper's own definition",
        ("compact.py", 'atomic_write(path, text, newline="")'): "inside that helper",
        ("history.py", 'atomic_write(chat_log, "".join(output))'):
            "tasks tag's chat_log rewrite — guarded by the chat-log hook's own "
            "lock file plus a content compare-and-swap, not the task lock",
        ("lifecycle.py", "atomic_write(session_state"):
            "the SESSION POINTER is a different resource (one file per session, "
            "written by `tasks work`); the guard re-reads it rather than locking",
        ("lifecycle.py", 'atomic_write(session_dir / "current_state"'):
            "the same session pointer",
        ("review.py", "atomic_write(judge_log"):
            "the judge LOG is a fresh per-review artifact, not a record other "
            "writers read-modify-write",
        ("review.py", "atomic_write("):
            "the hard-timeout partial log — a fresh file written once, and only "
            "on a clean tree (the tamper hard-stop exits before it)",
        ("merge_prep.py", "atomic_write(chat_log"):
            "offline merge preparation — single-process by construction",
        ("merge_prep.py", "atomic_write(state_file"):
            "offline merge preparation renumbering session pointers",
        ("merge_prep.py", "atomic_write(task_md"):
            "offline merge preparation — single-process by construction, and it "
            "renumbers whole task trees rather than editing a live record",
    }

    def test_every_direct_task_writer_is_approved(self):
        import re
        pkg = PLUGIN / "tasks"
        # Any direct write in these modules, whatever the variable is called
        # (058 impl panel r1, opus F3: a name list let `atomic_write(p, …)` or
        # `atomic_write(dest, …)` through while the docstring claimed otherwise).
        pattern = re.compile(r"(?:_)?atomic_write\(")
        unapproved = []
        # The modules that own task/judge records. Others (dashboard, mindmap,
        # intent, project_setup…) write their own files and are out of scope.
        RECORD_MODULES = {"core.py", "lifecycle.py", "review.py", "compact.py",
                          "history.py", "merge_prep.py"}
        for pyfile in sorted(pkg.glob("*.py")):
            if pyfile.name not in RECORD_MODULES:
                continue
            for i, line in enumerate(pyfile.read_text(encoding="utf-8").splitlines(), 1):
                if not pattern.search(line):
                    continue
                snippet = line.strip()
                if any(fname == pyfile.name and sub in snippet
                       for (fname, sub) in self.APPROVED):
                    continue
                unapproved.append(f"{pyfile.name}:{i}: {snippet[:100]}")
        self.assertEqual(unapproved, [], "\n".join(
            ["direct task/judge writers outside the approved set — route them through "
             "`atomic.rewrite`, or add them to APPROVED with the reason:", *unapproved]))

    def test_the_approved_set_has_no_dead_entries(self):
        # An approval that no longer matches anything is a stale exemption that
        # would silently cover a FUTURE writer with the same name.
        pkg = PLUGIN / "tasks"
        blobs = {f.name: f.read_text(encoding="utf-8") for f in pkg.glob("*.py")}
        dead = [f"{fname}: {sub}" for (fname, sub) in self.APPROVED
                if sub not in blobs.get(fname, "")]
        self.assertEqual(dead, [], f"stale approvals: {dead}")


class CloseRefusesARaceItWouldOverwrite(unittest.TestCase):
    """THE regression of task 058, with teeth.

    The measured baseline: the close validates, runs verify/judges for minutes,
    then commits — and a `tasks blocked` landing in that window was silently
    overwritten (5 of 5 trials; the task ended `in_progress` with no `## Blocked`
    section). Routing the writers alone does NOT fix this (a mutation check with
    the lock neutered still passed, because each writer now reads late); what
    fixes it is the commit RE-CHECKING what earned the close. These cases fail
    if `compose_close` stops refusing."""

    BASE = ("# 001 - T\n\n## Status\nin_progress\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1\n")

    def setUp(self):
        from tasks.core import CloseRaceRefused, compose_close, set_task_blocked
        self.compose_close = compose_close
        self.CloseRaceRefused = CloseRaceRefused
        self.set_task_blocked = set_task_blocked
        self.d = _tmp()
        self.tf = self.d / "task.md"
        self.tf.write_text(self.BASE, encoding="utf-8")

    def test_a_pause_that_landed_during_the_close_refuses_the_commit(self):
        self.set_task_blocked(self.tf, "waiting on the owner")     # the other session
        text = self.tf.read_text(encoding="utf-8")
        with self.assertRaises(self.CloseRaceRefused) as cm:
            self.compose_close(text, receipt_heading="Verification Receipt",
                               receipt="### closed\n", expect_status="in_progress")
        self.assertRegex(str(cm.exception), r"Blocked|status changed")

    def test_a_status_someone_else_changed_refuses_the_commit(self):
        text = self.BASE.replace("in_progress", "done")
        with self.assertRaises(self.CloseRaceRefused):
            self.compose_close(text, receipt_heading="Verification Receipt",
                               receipt="### closed\n", expect_status="in_progress")

    def test_an_undisturbed_close_commits_receipt_and_status_together(self):
        out = self.compose_close(self.BASE, receipt_heading="Verification Receipt",
                                 receipt="### closed\n", expect_status="in_progress")
        self.assertIn("## Verification Receipt", out)
        self.assertIn("### closed", out)
        self.assertRegex(out, r"## Status\ndone")

    def test_the_refusal_leaves_the_file_byte_identical(self):
        self.set_task_blocked(self.tf, "paused")
        before = self.tf.read_bytes()
        from tasks.atomic import rewrite
        with self.assertRaises(self.CloseRaceRefused):
            rewrite(self.tf, lambda t: self.compose_close(
                t, receipt_heading="Verification Receipt", receipt="### closed\n"))
        self.assertEqual(self.tf.read_bytes(), before)

    def test_end_to_end_a_pause_DURING_the_close_is_refused(self):
        """The real race through the real CLI: the close is mid-verify when
        another session pauses the task.

        The first version of this case paused BEFORE `work done` started, which
        is not a race at all — closing an already-blocked task is a legitimate
        operation, so it (correctly) succeeded and the test was meaningless. The
        pause now lands inside the verify window, which is the window the
        measured 5/5 baseline lost.
        """
        import json
        import threading
        d = _tmp()
        subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
        (d / ".agent").mkdir()
        # A verify command slow enough to pause inside.
        (d / ".agent" / "config.json").write_text(
            json.dumps({"verify": {"_always": [f'"{sys.executable}" -c "import time; time.sleep(4)"']}}),
            encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text(self.BASE, encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-058e2e")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

        def _pause_mid_verify():
            time.sleep(1.5)                       # inside the 4 s verify
            self.set_task_blocked(tf, "another session paused this")

        t = threading.Thread(target=_pause_mid_verify)
        t.start()
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "done"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=180)
        t.join()
        out = r.stdout + r.stderr
        self.assertNotIn("Task 001 done.", r.stdout, out)
        self.assertNotEqual(r.returncode, 0, out)
        text = tf.read_text(encoding="utf-8")
        self.assertIn("## Blocked", text, "the concurrent pause was overwritten")
        self.assertNotIn("\ndone\n", text.split("## Work Plan")[0],
                         "the task was closed over a live pause")


class ResumedTaskCanStillClose(unittest.TestCase):
    """058 impl panel r1, Critical (codex-medium #1 and grok #1, who ran the
    sequence): `resume_blocked_task` KEEPS the `## Blocked` section as history and
    only stamps `> Resumed`, so a close that refused on the section's PRESENCE
    bricked every block→resume→close flow — which is the whole handoff path. The
    refusal keys on a block that APPEARED or CHANGED since the close began."""

    BASE = ("# 001 - T\n\n## Status\nin_progress\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1\n")

    def setUp(self):
        from tasks.core import (CloseRaceRefused, blocked_digest, compose_close,
                                resume_blocked_task, set_task_blocked)
        self.CloseRaceRefused = CloseRaceRefused
        self.compose_close = compose_close
        self.blocked_digest = blocked_digest
        self.set_task_blocked = set_task_blocked
        self.resume_blocked_task = resume_blocked_task
        self.d = _tmp()
        self.tf = self.d / "task.md"
        self.tf.write_text(self.BASE, encoding="utf-8")

    def test_a_resumed_task_closes(self):
        self.set_task_blocked(self.tf, "waiting on the owner")
        self.resume_blocked_task(self.tf)
        text = self.tf.read_text(encoding="utf-8")
        out = self.compose_close(text, receipt_heading="Verification Receipt",
                                 receipt="### closed\n", expect_status="in_progress",
                                 expect_blocked=self.blocked_digest(text))
        self.assertRegex(out, r"## Status\ndone")
        self.assertIn("Resumed", out, "the resume history must survive the close")

    def test_a_pause_that_APPEARED_during_the_close_still_refuses(self):
        entry = self.tf.read_text(encoding="utf-8")
        entry_digest = self.blocked_digest(entry)        # None: no block at entry
        self.set_task_blocked(self.tf, "another session paused this")
        with self.assertRaises(self.CloseRaceRefused):
            self.compose_close(self.tf.read_text(encoding="utf-8"),
                               receipt_heading="Verification Receipt",
                               receipt="### closed\n", expect_status=None,
                               expect_blocked=entry_digest)

    def test_a_RE_block_during_the_close_refuses_even_after_a_resume(self):
        self.set_task_blocked(self.tf, "first pause")
        self.resume_blocked_task(self.tf)
        entry_digest = self.blocked_digest(self.tf.read_text(encoding="utf-8"))
        self.set_task_blocked(self.tf, "second pause")     # a NEW pause mid-close
        with self.assertRaises(self.CloseRaceRefused):
            self.compose_close(self.tf.read_text(encoding="utf-8"),
                               receipt_heading="Verification Receipt",
                               receipt="### closed\n", expect_status=None,
                               expect_blocked=entry_digest)

    def test_end_to_end_a_resumed_task_closes_through_the_cli(self):
        import json
        d = _tmp()
        subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(json.dumps({"verify": {"_always": []}}),
                                                  encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text(self.BASE, encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-resume")

        def run(*args):
            return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                                  cwd=d, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(run("work", "1").returncode, 0)
        self.assertEqual(run("blocked", "waiting on the owner").returncode, 0)
        self.assertEqual(run("work", "1").returncode, 0)          # resume
        r = run("work", "done")
        self.assertIn("Task 001 done.", r.stdout, r.stdout + r.stderr)


class RiskAndGatesAreRecheckedAtCommit(unittest.TestCase):
    """058 impl panel r1 (sonnet #2, codex ×2, grok #5): risk and gates are read
    BEFORE verify and the judges run — minutes — and the commit never compared
    them, so an edit in that window closed under the stale classification."""

    BASE = ("# 001 - T\n\n## Status\nin_progress\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1\n")

    def setUp(self):
        from tasks.core import CloseRaceRefused, compose_close
        self.CloseRaceRefused = CloseRaceRefused
        self.compose_close = compose_close

    def _close(self, text, **kw):
        base = dict(receipt_heading="Verification Receipt", receipt="### closed\n",
                    expect_status="in_progress")
        base.update(kw)
        return self.compose_close(text, **base)

    def test_a_risk_edited_during_the_close_refuses(self):
        changed = self.BASE.replace("reversible", "irreversible")
        with self.assertRaises(self.CloseRaceRefused) as cm:
            self._close(changed, expect_risk="reversible")
        self.assertIn("Risk", str(cm.exception))

    def test_an_unchanged_risk_commits(self):
        out = self._close(self.BASE, expect_risk="reversible")
        self.assertRegex(out, r"## Status\ndone")

    def test_a_gate_unchecked_during_the_close_refuses(self):
        reopened = self.BASE.replace("- [x] G1", "- [ ] G1")
        with self.assertRaises(self.CloseRaceRefused) as cm:
            self._close(reopened, expect_open_gates=0)
        self.assertIn("gate", str(cm.exception).lower())

    def test_unchanged_gates_commit(self):
        out = self._close(self.BASE, expect_open_gates=0)
        self.assertRegex(out, r"## Status\ndone")


class LockIdentity(unittest.TestCase):
    """058 impl panel r1 (opus F1 / sonnet #1, codex-high #4/#5)."""

    def test_depth_uses_the_same_key_as_the_holder(self):
        # The leak assertions in this module are vacuous if the two disagree —
        # which they did whenever the path resolves differently (macOS /var →
        # /private/var, symlinked temp dirs).
        d = _tmp()
        f = d / "task.md"
        f.write_text("x\n", encoding="utf-8")
        with filelock.task_lock(f):
            self.assertEqual(filelock._depth_for(f), 1, "the leak-proof helper is vacuous")
        self.assertEqual(filelock._depth_for(f), 0)

    def test_one_lock_per_task_DIRECTORY(self):
        # codex-high #5: a per-FILENAME lock let a panel write judge.md while a
        # close held task.md — the close reads judge evidence, so they must be
        # one lock.
        d = _tmp()
        (d / "task.md").write_text("x\n", encoding="utf-8")
        (d / "judge.md").write_text("y\n", encoding="utf-8")
        self.assertEqual(filelock.lock_path_for(d / "task.md"),
                         filelock.lock_path_for(d / "judge.md"))

    def test_another_THREAD_waits_rather_than_walking_in(self):
        # codex-high #4: re-entrancy must belong to the owning thread; a second
        # thread seeing the holder entry used to enter the protected region.
        import threading
        d = _tmp()
        f = d / "task.md"
        f.write_text("x\n", encoding="utf-8")
        overlapped = []

        def _other():
            try:
                with filelock.task_lock(f, timeout=0.4):
                    overlapped.append(True)
            except filelock.LockTimeout:
                overlapped.append(False)
        with filelock.task_lock(f):
            t = threading.Thread(target=_other)
            t.start()
            t.join(5)
        self.assertEqual(overlapped, [False], "a second thread walked into a held lock")


class LostUpdateRegression(unittest.TestCase):
    """The measured baseline of task 058, as a regression.

    Before: the real close shape — read task.md, run the verify contract for
    minutes, write the receipt — lost a concurrent `tasks blocked` in 5 of 5
    trials, and the task ended `in_progress` with NO `## Blocked` section.
    After: the pause survives, and status agrees with the section."""

    BASE = ("# 001 - T\n\n## Status\nin_progress\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1\n")

    def _slow_close(self, task_file, hold):
        """A close that reads, works for `hold` seconds, then commits."""
        return [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(PLUGIN)!r})
            from pathlib import Path
            from tasks.filelock import task_lock
            from tasks.core import upsert_task_section, _set_status
            tf = Path({str(task_file)!r})
            time.sleep(0.05)
            # The long work happens OUTSIDE the lock, exactly as the real close does.
            time.sleep({hold})
            with task_lock(tf):
                upsert_task_section(tf, "Verification Receipt", "### closed\\n")
                _set_status(tf, "done")
        """)]

    def _blocked(self, task_file, delay):
        return [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(PLUGIN)!r})
            from pathlib import Path
            from tasks.core import set_task_blocked
            time.sleep({delay})
            set_task_blocked(Path({str(task_file)!r}), "waiting on the owner")
        """)]

    def test_a_pause_landing_during_a_close_is_not_lost(self):
        lost = 0
        for _ in range(5):
            d = _tmp()
            tf = d / "task.md"
            tf.write_text(self.BASE, encoding="utf-8")
            a = subprocess.Popen(self._slow_close(tf, 0.30))
            b = subprocess.Popen(self._blocked(tf, 0.10))
            a.wait(); b.wait()
            text = tf.read_text(encoding="utf-8")
            if "## Blocked" not in text:
                lost += 1
        self.assertEqual(lost, 0, "a concurrent pause was silently dropped")

    def test_two_writers_keep_every_entry(self):
        d = _tmp()
        tf = d / "task.md"
        tf.write_text(self.BASE, encoding="utf-8")
        procs = [subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(PLUGIN)!r})
            from pathlib import Path
            from tasks.core import upsert_task_section
            tf = Path({str(tf)!r})
            for i in range(20):
                upsert_task_section(tf, "Verification Receipt", f"### P{w}-{{i}}\\n")
        """)]) for w in range(2)]
        for p in procs:
            p.wait()
        text = tf.read_text(encoding="utf-8")
        missing = [f"P{w}-{i}" for w in range(2) for i in range(20)
                   if f"P{w}-{i}" not in text]
        self.assertEqual(missing, [], f"{len(missing)}/40 entries lost")
        self.assertEqual(text.count("## Verification Receipt"), 1,
                         "the section was duplicated by a racing writer")

    def test_a_crash_between_transform_and_write_leaves_the_file_intact(self):
        d = _tmp()
        tf = d / "task.md"
        tf.write_text(self.BASE, encoding="utf-8")
        before = tf.read_bytes()
        crasher = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {str(PLUGIN)!r})
            from pathlib import Path
            from tasks.atomic import rewrite
            def t(text):
                os._exit(9)          # die holding the lock, mid-transaction
            rewrite(Path({str(tf)!r}), t)
        """)])
        self.assertNotEqual(crasher.returncode, 0)
        self.assertEqual(tf.read_bytes(), before, "the crash left a torn file")
        # …and the lock the dead process held is reclaimable.
        with filelock.task_lock(tf, timeout=5.0):
            pass


class GuardReadsOnceAndRechecksThePointer(unittest.TestCase):
    """058 plan panel P2: the destructive-command guard read task.md twice (once
    for the status, again inside `extract_risk`), so a write between them could
    pair one task's status with another's risk; and it cannot take the task lock
    (a PreToolUse hook must never wait on a writer). It now reads once and
    re-reads the POINTER, and every error keeps the command BLOCKED."""

    def _project(self, risk="irreversible", status="in_progress", num="001"):
        d = _tmp()
        agent = d / ".agent"
        (agent / "sessions" / "pid-1").mkdir(parents=True)
        # The real pointer is the ZERO-PADDED number (checked against this
        # workspace's own `.agent/sessions/<sid>/current_state`).
        (agent / "sessions" / "pid-1" / "current_state").write_text(num, encoding="utf-8")
        td = agent / "tasks" / f"{num}-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            f"# {num}\n\n## Status\n{status}\n\n## Risk\n{risk}\n", encoding="utf-8")
        return d

    def _guard(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "cg_under_test", PLUGIN / "scripts" / "command_guard.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_an_irreversible_active_task_acknowledges(self):
        d = self._project()
        g = self._guard()
        with mock.patch.dict(os.environ, {"PLAYBOOK_SESSION_ID": "pid-1"}):
            self.assertTrue(g._active_task_is_irreversible(str(d)))

    def test_a_reversible_one_does_not(self):
        d = self._project(risk="reversible")
        g = self._guard()
        with mock.patch.dict(os.environ, {"PLAYBOOK_SESSION_ID": "pid-1"}):
            self.assertFalse(g._active_task_is_irreversible(str(d)))

    def test_a_task_switch_mid_check_refuses(self):
        # The pointer moves to another task WHILE the guard is reading: the stale
        # irreversible task must not acknowledge for the new one.
        d = self._project()
        g = self._guard()
        pointer = d / ".agent" / "sessions" / "pid-1" / "current_state"
        real_read = Path.read_text
        state = {"n": 0}

        def _switch_once(self_path, *a, **k):
            out = real_read(self_path, *a, **k)
            if self_path.name == "task.md" and state["n"] == 0:
                state["n"] = 1
                pointer.write_text("002", encoding="utf-8")   # another session switched
            return out
        with mock.patch.dict(os.environ, {"PLAYBOOK_SESSION_ID": "pid-1"}), \
             mock.patch.object(Path, "read_text", _switch_once):
            self.assertFalse(g._active_task_is_irreversible(str(d)),
                             "a stale pointer acknowledged after a task switch")

    def test_every_error_keeps_the_command_blocked(self):
        g = self._guard()
        with mock.patch.dict(os.environ, {"PLAYBOOK_SESSION_ID": "pid-1"}):
            self.assertFalse(g._active_task_is_irreversible("/nonexistent/root"))
            self.assertFalse(g._active_task_is_irreversible(""))

    def test_the_guard_takes_no_task_lock(self):
        # Structural: a hook that waited on a minutes-long close would stall the
        # session, so the guard must not import or call the lock at all.
        src = (PLUGIN / "scripts" / "command_guard.py").read_text(encoding="utf-8")
        self.assertNotIn("task_lock", src)
        self.assertNotIn("filelock", src)


class TagDoesNotLoseAnAppendedPrompt(unittest.TestCase):
    """058 plan panel P11 (and 073's parked item): `tasks tag` REWRITES
    chat_log.md from a buffer it read earlier, while the chat-log hook appends to
    the same file — so a prompt arriving in between was silently dropped. The
    rewrite now rendezvous on the hook's own lock file AND compare-and-swaps the
    content, writing nothing rather than losing a message."""

    def _project(self):
        d = _tmp()
        agent = d / ".agent"
        agent.mkdir()
        (agent / "chat_log.md").write_text(
            "**[M001]** [2026-09-22 08:00:00 UTC]\nfirst\n", encoding="utf-8")
        (agent / "bash_history").write_text(
            "2026-09-22 08:00:00 | claude | .claude/bin/tasks work 1\n", encoding="utf-8")
        return d

    def test_an_undisturbed_tag_run_writes_its_tags(self):
        # The negative control for the refusal above.
        import contextlib
        import io
        from tasks import history
        d = self._project()
        chat = d / ".agent" / "chat_log.md"
        cwd = os.getcwd()
        os.chdir(d)
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with contextlib.suppress(SystemExit):
                    history.cmd_tag([])
        finally:
            os.chdir(cwd)
        self.assertIn("<!-- T001 -->", chat.read_text(encoding="utf-8"))

    def test_the_hook_holds_the_lock_across_its_append(self):
        # 058 impl panel r1 (codex-high #1, grok #4): the hook locked only the
        # COUNTER and released before appending, so an append begun during a tag
        # rewrite was replaced away with it. Both sides now hold the same lock.
        hook = (PLUGIN / "scripts" / "chat-log-hook").read_text(encoding="utf-8")
        self.assertIn("append_message", hook)
        self.assertIn('201>"$COUNTER_FILE.lock"', hook)
        appended = hook.index("append_message() {")
        locked = hook.index("flock -x 201")
        self.assertLess(appended, locked, "the append is not inside the lock")

    def test_the_rewrite_rendezvous_on_the_hooks_lock_file(self):
        # The shell hook locks `<agent>/chat_log_counter.lock`; the Python
        # rewriter must use the SAME path or the two protocols never meet.
        d = self._project()
        src = (PLUGIN / "tasks" / "history.py").read_text(encoding="utf-8")
        self.assertIn('"chat_log_counter.lock"', src)
        self.assertIn("named_lock(", src)
        hook = (PLUGIN / "scripts" / "chat-log-hook").read_text(encoding="utf-8")
        self.assertIn('200>"$counter_file.lock"', hook)

    def test_tag_refuses_when_the_log_changed_under_it(self):
        # Deterministic: the append happens exactly in the window the guard
        # protects — after the tag buffer was read, as the lock is taken. A
        # thread-and-sleep race is not a test (the first version of this case
        # "passed" only because its daemon thread died at process exit, so there
        # was nothing to lose).
        import contextlib
        import io
        from tasks import history
        d = self._project()
        chat = d / ".agent" / "chat_log.md"
        real_lock = history.__dict__.get("task_lock")

        @contextlib.contextmanager
        def _lock_and_append(path, *a, **k):
            chat.write_text(chat.read_text(encoding="utf-8") +
                            "**[M099]** [2026-09-22 09:00:00 UTC]\nlate\n",
                            encoding="utf-8")          # the hook appends here
            yield

        cwd = os.getcwd()
        err = io.StringIO()
        code = None
        with mock.patch("tasks.filelock.named_lock", _lock_and_append):
            os.chdir(d)
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    history.cmd_tag([])
            except SystemExit as e:
                code = e.code
            finally:
                os.chdir(cwd)
        self.assertEqual(code, 1, "the stale rewrite was not refused")
        self.assertIn("changed while tags were being computed", err.getvalue())
        text = chat.read_text(encoding="utf-8")
        self.assertIn("M099", text, "the concurrently appended prompt was lost")
        self.assertNotIn("<!-- T001 -->", text, "a stale rewrite landed anyway")


class JudgeRoundsSurviveConcurrentPanels(unittest.TestCase):
    """058 impl panel r1 (opus F2): the judge stacker was put under the lock but
    nothing proved it — the only two-process interleave was on
    `upsert_task_section`, while judge.md concurrency was the original defect."""

    def test_two_processes_stacking_rounds_lose_none(self):
        d = _tmp()
        jm = d / "judge.md"
        jm.write_text("", encoding="utf-8")
        procs = [subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(PLUGIN)!r})
            from pathlib import Path
            from tasks.core import stack_judge_round
            jm = Path({str(jm)!r})
            for i in range(6):
                stack_judge_round(jm, f"# Panel Impl Review — t\\n\\n"
                                      f"**PANEL VERDICT: PASS** — P{w} round {{i}}\\n")
        """)]) for w in range(2)]
        for p in procs:
            p.wait()
        text = jm.read_text(encoding="utf-8")
        archive = (d / "judge-archive.md")
        both = text + (archive.read_text(encoding="utf-8") if archive.exists() else "")
        missing = [f"P{w} round {i}" for w in range(2) for i in range(6)
                   if f"P{w} round {i}" not in both]
        self.assertEqual(missing, [], f"{len(missing)}/12 paid rounds lost")


class Round2Fixes(unittest.TestCase):
    """058 impl panel round 2."""

    BASE = ("# 001 - T\n\n## Status\nin_progress\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1\n")

    def test_a_failed_entry_snapshot_skips_the_checks_rather_than_asserting_none(self):
        # opus F2: the `except` branch set `_blocked_at_entry = None`, which
        # ENFORCES "there was no block" — so a transient read failure on a
        # RESUMED task (which legitimately keeps its `## Blocked` history) would
        # falsely refuse, re-breaking exactly what round 1 fixed.
        import inspect
        from tasks import lifecycle
        src = inspect.getsource(lifecycle)
        i = src.index("_status_at_entry = _es058")
        window = src[i:i + 900]
        self.assertIn("_UNSET", window,
                      "a failed entry snapshot must SKIP the comparisons, not assert a baseline")

    def test_compose_close_skips_a_check_it_was_given_no_baseline_for(self):
        from tasks.core import compose_close, set_task_blocked, resume_blocked_task
        d = _tmp()
        tf = d / "task.md"
        tf.write_text(self.BASE, encoding="utf-8")
        set_task_blocked(tf, "paused")
        resume_blocked_task(tf)
        # No `expect_blocked` at all → the resumed task still closes.
        out = compose_close(tf.read_text(encoding="utf-8"),
                            receipt_heading="Verification Receipt",
                            receipt="### closed\n", expect_status="in_progress")
        self.assertRegex(out, r"## Status\ndone")

    def test_lock_timeout_prints_its_message_instead_of_a_traceback(self):
        # opus F1: the crafted LockTimeout message was never caught at the CLI
        # boundary, so a contended lock surfaced as a Python traceback.
        d = _tmp()
        subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
        (d / ".agent").mkdir()
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text(self.BASE, encoding="utf-8")
        if filelock.selected_backend() == "none":
            self.skipTest("no locking backend on this platform")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-to",
                   PLAYBOOK_LOCK_TIMEOUT_SECS="1")
        # Activate BEFORE the holder takes the lock (activation writes too).
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(PLUGIN)!r})
                from tasks import filelock
                with filelock.task_lock({str(tf)!r}):
                    print("HELD", flush=True)
                    time.sleep(20)
            """)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "HELD")
            r = subprocess.run([sys.executable, "-m", "tasks.cli", "blocked", "waiting"],
                               cwd=d, env=env, capture_output=True, text=True, timeout=120)
            out = r.stdout + r.stderr
            self.assertNotIn("Traceback", out, out[-400:])
            self.assertIn("has held", out)
            self.assertNotEqual(r.returncode, 0)
        finally:
            holder.kill()
            holder.wait()

    def test_a_superseded_panel_verdict_refuses_the_close(self):
        # codex-high #1 / codex-medium #1 (Critical): the close reads judge.md's
        # newest verdict BEFORE verify; a panel installing a newer FAIL in that
        # window was never re-checked at the commit.
        from tasks.core import CloseRaceRefused, compose_close, judge_digest
        d = _tmp()
        tf = d / "task.md"
        tf.write_text(self.BASE, encoding="utf-8")
        jm = d / "judge.md"
        jm.write_text("# Panel Impl Review — t\n\n**PANEL VERDICT: PASS** — 5/5\n", encoding="utf-8")
        entry = judge_digest(tf)
        jm.write_text("# Panel Impl Review — t\n\n**PANEL VERDICT: FAIL** — 2/5\n"
                      "\n# Panel Impl Review — t\n\n**PANEL VERDICT: PASS** — 5/5\n",
                      encoding="utf-8")
        with self.assertRaises(CloseRaceRefused) as cm:
            compose_close(tf.read_text(encoding="utf-8"),
                          receipt_heading="Verification Receipt", receipt="### c\n",
                          expect_status="in_progress", expect_judge=entry,
                          task_file=tf)
        self.assertIn("panel", str(cm.exception).lower())

    def test_an_unchanged_panel_verdict_commits(self):
        from tasks.core import compose_close, judge_digest
        d = _tmp()
        tf = d / "task.md"
        tf.write_text(self.BASE, encoding="utf-8")
        (d / "judge.md").write_text("# Panel Impl Review — t\n\n**PANEL VERDICT: PASS** — 5/5\n",
                                    encoding="utf-8")
        out = compose_close(tf.read_text(encoding="utf-8"),
                            receipt_heading="Verification Receipt", receipt="### c\n",
                            expect_status="in_progress", expect_judge=judge_digest(tf),
                            task_file=tf)
        self.assertRegex(out, r"## Status\ndone")

    def test_a_second_close_of_an_already_done_task_refuses(self):
        # codex-high #2: the loser of two concurrent closes captured `done` as
        # its own expected status and appended a second receipt.
        from tasks.core import CloseRaceRefused, compose_close
        done = self.BASE.replace("in_progress", "done")
        with self.assertRaises(CloseRaceRefused) as cm:
            compose_close(done, receipt_heading="Verification Receipt",
                          receipt="### c\n", expect_status="done")
        self.assertIn("already", str(cm.exception).lower())

    def test_the_state_echo_hook_appends_under_the_shared_lock(self):
        # codex-high #3: the gate logger appends to chat_log.md too, so a
        # `tasks tag` rewrite could lose a gate entry.
        hook = (PLUGIN / "scripts" / "state-echo-hook").read_text(encoding="utf-8")
        self.assertIn("chat_log_counter.lock", hook)
        self.assertIn("flock -x", hook)

    def test_compact_refuses_a_stale_compose(self):
        # sonnet #2 + codex ×2: compact composed its new text BEFORE taking the
        # lock, so a writer landing in between was overwritten — the same shape
        # this task measured 5/5. Driven through the real command.
        import contextlib
        import io
        from tasks import compact as C
        d = _tmp()
        agent = d / ".agent" / "tasks" / "001-t"
        agent.mkdir(parents=True)
        tf = agent / "task.md"
        tf.write_text("# 001\n\n## Status\nin_progress\n\n## Notes\n"
                      "<!-- archive:start -->\ncold narrative\n<!-- archive:end -->\n",
                      encoding="utf-8")
        real_lock = C.__dict__.get("task_lock")

        @contextlib.contextmanager
        def _lock_and_write(path, *a, **k):
            tf.write_text(tf.read_text(encoding="utf-8") +
                          "\n## Blocked\n> another session paused this\n", encoding="utf-8")
            yield

        cwd = os.getcwd()
        code = None
        err = io.StringIO()
        with mock.patch("tasks.filelock.task_lock", _lock_and_write):
            os.chdir(d)
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    C.cmd_compact(["001"])
            except SystemExit as e:
                code = e.code
            finally:
                os.chdir(cwd)
        self.assertEqual(code, 1, "a stale compaction was written")
        self.assertIn("changed while the compaction was being composed", err.getvalue())
        text = tf.read_text(encoding="utf-8")
        self.assertIn("## Blocked", text, "the concurrent write was overwritten")
        self.assertIn("cold narrative", text, "blocks were moved despite the refusal")

    def test_compact_still_works_undisturbed(self):
        import contextlib
        import io
        from tasks import compact as C
        d = _tmp()
        agent = d / ".agent" / "tasks" / "001-t"
        agent.mkdir(parents=True)
        tf = agent / "task.md"
        tf.write_text("# 001\n\n## Status\nin_progress\n\n## Notes\n"
                      "<!-- archive:start -->\ncold narrative\n<!-- archive:end -->\n",
                      encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(d)
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with contextlib.suppress(SystemExit):
                    C.cmd_compact(["001"])
        finally:
            os.chdir(cwd)
        self.assertIn("compacted", tf.read_text(encoding="utf-8").lower())
        self.assertIn("cold narrative",
                      (agent / "task-archive.md").read_text(encoding="utf-8"))

    def test_the_tamper_exemption_has_no_dead_entry(self):
        # sonnet #3: `judge.md.lock` was exempted but nothing ever creates it —
        # the lock is one per DIRECTORY, always `task.md.lock`.
        review_src = (PLUGIN / "tasks" / "review.py").read_text(encoding="utf-8")
        self.assertNotIn('"judge.md.lock"', review_src)
        self.assertIn('"task.md.lock"', review_src)


class LockFileIsNotTamper(unittest.TestCase):
    """058 plan panel P3: my Design gate ASSUMED the lock file was tamper-exempt
    because it lives under `.agent/`. It was not — task 059's guard exempts only
    EXACT record names in a TRACKED task dir, so a lock file first created during
    a panel window would have surfaced as `?? …/task.md.lock` and voided a paid
    panel. Asserted here instead of assumed."""

    def test_a_lock_file_created_during_a_panel_is_not_a_mutation(self):
        import subprocess as sp
        from tasks import review as R
        d = _tmp()
        sp.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
        sp.run(["git", "-C", str(d), "config", "user.email", "x@y.z"], check=True, capture_output=True)
        sp.run(["git", "-C", str(d), "config", "user.name", "x"], check=True, capture_output=True)
        td = d / ".agent" / "tasks" / "001-x"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text("# 001\n\n## Status\nin_progress\n", encoding="utf-8")
        sp.run(["git", "-C", str(d), "add", "-A"], check=True, capture_output=True)
        sp.run(["git", "-C", str(d), "commit", "-qm", "tracked task dir"], check=True, capture_output=True)
        before = R._snapshot_repo_state(d, tf)
        with filelock.task_lock(tf):          # the first write of the panel window
            pass
        self.assertTrue(filelock.lock_path_for(tf).exists())
        full = R._detect_tamper_full(d, tf, before)
        self.assertEqual(full["mutations"], [], full)

    def test_the_seeded_gitignore_covers_it(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "cmm", PLUGIN / "scripts" / "claude-md-merge.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn(".agent/tasks/*/*.lock", mod.GITIGNORE_ENTRIES)
        self.assertIn(".agent/*/tasks/*/*.lock", mod.GITIGNORE_ENTRIES)


if __name__ == "__main__":
    unittest.main()
