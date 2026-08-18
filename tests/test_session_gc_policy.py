"""The session-liveness policy, and the two Python consumers that share it (task 027).

`current_state` is written ONLY by `tasks work <N>`, so its mtime records when a
task was ACTIVATED, not when the session was last alive. Treating it as liveness
deleted the live session's own pointer for any task active >24h — silently
revoking code-edit permission mid-task, because task-gate-hook then blocks
Edit/Write. Three consumers had to agree afterwards:

  * `_gc_dead_sessions`   — deletes  (this file)
  * `tasks doctor`        — reports  (this file)
  * `scripts/session-start-hook` — deletes, in bash (S18 in
    tests/wrapper-multiuser-fixture.sh asserts bash/python parity)

so the policy lives in ONE predicate, `_session_is_dead`, and these tests pin its
matrix plus each consumer's use of it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"

sys.path.insert(0, str(PLUGIN))

import tasks.shared as shared  # noqa: E402  (the 1.5.9 split moved the GC policy out of cli.py)

DAY = 86400


def reaped_pid() -> int:
    """A pid that is certainly dead AND reaped.

    `wait()` is what makes it reaped: a zombie still answers `kill -0`, which
    would silently make every "dead session is removed" assertion vacuous. The
    liveness re-check guards the pid-reuse window instead of trusting it.
    """
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = p.pid
    p.wait()
    try:
        os.kill(pid, 0)
    except OSError:
        return pid
    raise unittest.SkipTest(f"pid {pid} was reused while the test ran")


class TestSessionIsDead(unittest.TestCase):
    """The predicate itself: pid names by liveness only, legacy names by mtime."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cutoff = time.time() - DAY

    def make(self, name: str, pointer_age_s: float | None = 0.0) -> Path:
        d = self.sessions / name
        d.mkdir(parents=True)
        if pointer_age_s is not None:
            f = d / "current_state"
            f.write_text("001\n", encoding="utf-8")
            t = time.time() - pointer_age_s
            os.utime(f, (t, t))
        return d

    def dead(self, d: Path, own: str = "") -> bool:
        return shared._session_is_dead(d, own, self.cutoff)

    def test_own_session_is_never_dead_even_with_an_ancient_pointer(self):
        """The field case: a live session two days into one task."""
        d = self.make("pid-4242", pointer_age_s=2 * DAY)
        self.assertFalse(self.dead(d, own="pid-4242"))

    def test_own_session_is_kept_even_when_its_name_fails_kill(self):
        """`pid-win-fallback` is the Windows constant: non-numeric, so liveness
        can never vouch for it. Self-exclusion is its ONLY keep path."""
        d = self.make("pid-win-fallback", pointer_age_s=2 * DAY)
        self.assertFalse(self.dead(d, own="pid-win-fallback"))
        self.assertTrue(self.dead(d, own="pid-999"), "a NON-own one must be reclaimed")

    def test_live_foreign_pid_is_kept_regardless_of_pointer_age(self):
        d = self.make(f"pid-{os.getppid()}", pointer_age_s=5 * DAY)
        self.assertFalse(self.dead(d))

    def test_dead_pid_is_reclaimed_even_with_a_fresh_pointer(self):
        """mtime must not rescue a dead session — the inverse of the bug."""
        d = self.make(f"pid-{reaped_pid()}", pointer_age_s=0)
        self.assertTrue(self.dead(d))

    def test_non_numeric_pid_name_is_reclaimed(self):
        self.assertTrue(self.dead(self.make("pid-12ab")))
        self.assertTrue(self.dead(self.make("pid-")))

    def test_pid_dir_without_a_pointer_follows_liveness_not_absence(self):
        """A just-provisioned session has no pointer until `tasks work <N>`."""
        self.assertFalse(self.dead(self.make(f"pid-{os.getpid()}", pointer_age_s=None)))
        self.assertTrue(self.dead(self.make(f"pid-{reaped_pid()}", pointer_age_s=None)))

    def test_huge_numeric_pid_does_not_crash(self):
        """`int()` accepts it but os.kill overflows C pid_t, and OverflowError is
        NOT an OSError — with it uncaught, ONE such directory made every `tasks`
        invocation die with a traceback, since the GC runs at CLI entry."""
        d = self.make("pid-99999999999999999999")
        self.assertTrue(self.dead(d))

    def test_live_process_owned_by_another_user_is_kept(self):
        """EPERM means the process EXISTS. Reclaiming it would be cross-user data
        loss, and the old mtime-only sweep kept such a session."""
        if os.getuid() == 0:
            self.skipTest("root gets no EPERM")
        d = self.make("pid-1", pointer_age_s=5 * DAY)   # init/launchd: alive, not ours
        self.assertFalse(self.dead(d))

    def test_legacy_name_uses_the_24h_mtime_fallback(self):
        self.assertFalse(self.dead(self.make("uuid-fresh", pointer_age_s=60)))
        self.assertTrue(self.dead(self.make("uuid-stale", pointer_age_s=2 * DAY)))
        self.assertTrue(self.dead(self.make("default", pointer_age_s=None)))


class TestGcSelfExclusionWithoutEnv(unittest.TestCase):
    """PLAYBOOK_SESSION_ID does not always propagate; self-exclusion must survive that."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "proj"
        self.sessions = self.project / ".agent" / "sessions"
        self.sessions.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def populate(self, names_with_age):
        for name, age in names_with_age:
            d = self.sessions / name
            d.mkdir(parents=True, exist_ok=True)
            f = d / "current_state"
            f.write_text("001\n", encoding="utf-8")
            t = time.time() - age
            os.utime(f, (t, t))

    def test_windows_constant_session_survives_when_env_is_unset(self):
        """Without the fallback to resolve_session_id(), own_session was "" and
        `int("win-fallback")` raised → the shared Windows session dir was deleted
        at EVERY CLI invocation."""
        self.populate([
            ("pid-win-fallback", 2 * DAY),          # own, non-numeric, stale
            (f"pid-{reaped_pid()}", 0),             # dead, fresh pointer
            ("pid-garbage", 0),
            ("uuid-old", 2 * DAY),
            ("uuid-new", 60),
        ])
        env_backup = os.environ.pop("PLAYBOOK_SESSION_ID", None)
        real = shared.resolve_session_id
        shared.resolve_session_id = lambda: "pid-win-fallback"
        try:
            shared._gc_dead_sessions(self.project)
        finally:
            shared.resolve_session_id = real
            if env_backup is not None:
                os.environ["PLAYBOOK_SESSION_ID"] = env_backup
        self.assertEqual(sorted(p.name for p in self.sessions.iterdir()),
                         ["pid-win-fallback", "uuid-new"])

    def test_env_var_still_wins_when_present(self):
        self.populate([("uuid-mine", 2 * DAY), ("uuid-other", 2 * DAY)])
        os.environ["PLAYBOOK_SESSION_ID"] = "uuid-mine"
        try:
            shared._gc_dead_sessions(self.project)
        finally:
            os.environ.pop("PLAYBOOK_SESSION_ID", None)
        self.assertEqual([p.name for p in self.sessions.iterdir()], ["uuid-mine"])


class TestDoctorReportsTheSamePolicy(unittest.TestCase):
    """doctor must never report a session the GC would keep.

    Note what `doctor` can actually observe: `_gc_dead_sessions` runs at the CLI
    entry point, so every DELETABLE dead dir is gone before the check looks. The
    surviving signal is therefore "dead, and the sweep could not reclaim it",
    which is exactly worth reporting because the sweep is deliberately silent
    about its own failures.
    """

    def build(self, own: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name) / "proj"
        (project / ".agent" / "tasks").mkdir(parents=True)
        sessions = project / ".agent" / "sessions"
        cases = {
            own: 2 * DAY,                   # own + ancient pointer → kept
            f"pid-{os.getpid()}": 2 * DAY,  # live foreign          → kept
            "uuid-fresh": 60,               # legacy + fresh        → kept
        }
        for name, age in cases.items():
            d = sessions / name
            d.mkdir(parents=True)
            f = d / "current_state"
            f.write_text("001\n", encoding="utf-8")
            t = time.time() - age
            os.utime(f, (t, t))
        return project

    def run_doctor(self, project: Path, own: str) -> str:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN)
        env["PLAYBOOK_SESSION_ID"] = own
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "doctor"],
                           cwd=project, env=env, capture_output=True, text=True)
        line = next((l for l in r.stdout.splitlines() if "dead session dirs" in l), "")
        self.assertTrue(line, f"doctor printed no session check:\n{r.stdout}")
        return line

    def test_live_sessions_are_never_reported(self):
        """The regression that matters: before task 027 the own session and any
        long-running session were reported as stale purely for pointer age."""
        own = "pid-doctor-own"
        line = self.run_doctor(self.build(own), own)
        self.assertIn("clean", line, f"doctor reported live sessions: {line}")
        self.assertNotIn(own, line, "doctor reported OUR OWN live session")
        self.assertNotIn(f"pid-{os.getpid()}", line,
                         "doctor reported a live foreign session")

    # getattr, not os.geteuid(): the attribute does not exist on native Windows,
    # so calling it at class-definition time would fail COLLECTION of this whole
    # module — on the platform whose session bug half these tests cover.
    @unittest.skipIf(getattr(os, "geteuid", lambda: 1)() == 0,
                     "chmod cannot block root's rmtree")
    @unittest.skipIf(sys.platform.startswith("win"),
                     "POSIX permission semantics unavailable")
    def test_an_unreclaimable_dead_session_is_still_reported(self):
        """Proves the check still has teeth: without this, "clean" above could
        just mean the check never fires."""
        own = "pid-doctor-own"
        project = self.build(own)
        sessions = project / ".agent" / "sessions"
        dead = sessions / f"pid-{reaped_pid()}"
        dead.mkdir(parents=True)
        (dead / "current_state").write_text("001\n", encoding="utf-8")
        dead.chmod(0o500)                       # child cannot be unlinked
        self.addCleanup(lambda: dead.chmod(0o700))
        line = self.run_doctor(project, own)
        self.assertIn(dead.name, line,
                      "a dead session the sweep could not reclaim went unreported")
        self.assertNotIn(own, line)


class TestSymlinkSafety(unittest.TestCase):
    """A symlink in sessions/ must never be followed.

    `rm -rf "link/"` follows it: measured on macOS, the TARGET directory was
    deleted and the symlink left behind. rmtree refuses, so this also pins the
    bash/python parity the fix relies on.
    """

    def test_gc_does_not_delete_a_symlink_target(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        target = root / "precious"
        target.mkdir()
        (target / "keepme.txt").write_text("PRECIOUS", encoding="utf-8")
        project = root / "proj"
        sessions = project / ".agent" / "sessions"
        sessions.mkdir(parents=True)
        # Named so the policy would classify it dead if it were a real dir.
        (sessions / "pid-12ab").symlink_to(target, target_is_directory=True)

        os.environ["PLAYBOOK_SESSION_ID"] = "pid-own"
        try:
            shared._gc_dead_sessions(project)
        finally:
            os.environ.pop("PLAYBOOK_SESSION_ID", None)

        self.assertTrue(target.exists(), "the symlink TARGET was deleted")
        self.assertTrue((target / "keepme.txt").exists(),
                        "contents behind the symlink were deleted")


if __name__ == "__main__":
    unittest.main()
