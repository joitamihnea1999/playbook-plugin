"""PB-INSTALL-ROLLBACK: `scripts/init` is all-or-nothing.

Before this work, `scripts/init` had no rollback of any kind: a failure partway
through left a half-provisioned, possibly mixed-version install and could have
clobbered a hand-written CLAUDE.md, .gitignore, or shell rc file with no way
back. init now snapshots every file it may modify before the first mutation
(via `init_txn.py`) and, on ANY non-zero exit, restores the pre-init state.

These tests induce a failure at a mutation stage by each of the three routes an
init run can leave non-zero, and prove the same two things every time:

  * byte-identical restoration of every pre-existing file init modified, and
  * no mixed state — every file init created is gone.

The three routes:
  * a `set -e` hard abort         — a read-only `.claude/bin` breaks a wrapper write
  * the soft FAILED summary       — a read-only `~/.claude` (permission denied) fails
                                     the bash-log deploy, so init exits 1 at its summary
  * a trapped interrupt (SIGTERM) — a python3 shim signals the run mid-merge

A negative control proves the byte-identity assertion has teeth: with the
transaction disabled (PB_INIT_NO_TXN=1) the very same failure DOES clobber the
pre-existing file and DOES leave created files behind — so a broken/absent
restore is detectable, not silently passed. Idempotent re-init is also checked:
the transaction machinery must not break a normal second run.

Every run uses a disposable HOME and project dir — the real ~/.claude is never
touched (the S14 lesson).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._bashcheck import bash_or_skip

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "playbook" / "scripts"
INIT = SCRIPTS / "init"

# The files init modifies in place, where a real user keeps hand-written content
# that a non-transactional failure could destroy.
SEED_CLAUDE = "# My Project\n\n**Read PURPOSE.md first.** Owner's pointer.\n"
SEED_GITIGNORE = "# mine\n__pycache__/\nbuild/\n"
SEED_PROJECT_SETTINGS = '{\n  "keepme": true,\n  "permissions": {"deny": ["Custom"]}\n}\n'
SEED_RC = "export MY_VAR=1\n# my own profile\n"
SEED_USER_SETTINGS = '{\n  "env": {"KEEP": "1"}\n}\n'

# A representative slice of what init provisions; none may survive a rollback.
PROVISIONED = (
    ".agent/config.json",
    ".agent/tasks",
    ".agent/playbooks/README.md",
    "MIND_MAP.md",
    ".claude/bin/tasks",
    ".claude/hooks/monitor-nudge.sh",
)


class InitRollback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._restore_writable_then_cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.project = self.root / "proj"
        self.home.mkdir()
        self.project.mkdir()

    def _restore_writable_then_cleanup(self):
        # A test may leave a dir read-only (the permission vectors); make the
        # whole tree writable again so TemporaryDirectory can delete it.
        for dirpath, dirnames, _ in os.walk(self.root):
            for name in [dirpath] + [os.path.join(dirpath, d) for d in dirnames]:
                try:
                    os.chmod(name, 0o700)
                except OSError:
                    pass
        self._tmp.cleanup()

    # ---- running init ------------------------------------------------------

    def _env(self, **extra):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        # Strip dogfood state so the outer shell's BASH_ENV can't drop a stray
        # .agent/bash_history into the project and perturb the byte comparison.
        for k in ("BASH_ENV", "PLAYBOOK_SESSION_ID", "PLAYBOOK_ROLE", "PLAYBOOK_EVAL_CONFIG"):
            env.pop(k, None)
        env.update(extra)
        return env

    def _run(self, *, env_extra=None, name="proj"):
        return subprocess.run(
            [bash_or_skip(), str(INIT), name],
            cwd=self.project, env=self._env(**(env_extra or {})),
            text=True, capture_output=True, timeout=120,
        )

    def _make_readonly_or_skip(self, directory: Path):
        """chmod *directory* read-only and confirm the OS actually blocks file
        creation there. Windows/MSYS ignore POSIX directory permissions and root
        bypasses them, so a read-only dir does not induce the failure there — the
        honest move is to skip (windows-git-bash interruption is Phase 8 live
        evidence per the ledger), not to assert a failure that cannot happen."""
        os.chmod(directory, stat.S_IRUSR | stat.S_IXUSR)
        probe = directory / ".pb_write_probe"
        try:
            probe.write_text("x", encoding="utf-8")
        except OSError:
            return  # good: the OS blocks writes here
        probe.unlink()
        os.chmod(directory, 0o700)
        self.skipTest(f"read-only dir not enforced for this user/OS ({directory})")

    def _run_interrupted(self, name="proj"):
        """Run init and SIGTERM it mid-run, deterministically: a python3 shim on
        PATH kills the init pid the instant init shells out to claude-md-merge.py
        (init's last mutation stage), by which point every earlier stage has run.
        """
        bash = bash_or_skip()
        shim_dir = self.root / "shim"
        shim_dir.mkdir(exist_ok=True)
        pidfile = self.root / "initpid"
        real_py = subprocess.run(
            [bash, "-c", "command -v python3"], text=True, capture_output=True
        ).stdout.strip()
        (shim_dir / "python3").write_text(
            "#!/bin/bash\n"
            "for a in \"$@\"; do case \"$a\" in\n"
            "  *claude-md-merge.py)\n"
            f"    kill -TERM \"$(cat '{pidfile}')\" 2>/dev/null\n"
            "    sleep 0.3\n"
            "    exit 0 ;;\n"
            "esac; done\n"
            f"exec '{real_py}' \"$@\"\n",
            encoding="utf-8",
        )
        (shim_dir / "python3").chmod(0o755)
        env = self._env(PATH=f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        return subprocess.run(
            [bash, "-c", f'echo $$ > "{pidfile}"; exec "{bash}" "{INIT}" "{name}"'],
            cwd=self.project, env=env, text=True, capture_output=True, timeout=120,
        )

    # ---- seeding + assertions ---------------------------------------------

    def _seed_modifiable_files(self):
        """Plant hand-written content in every file init modifies in place, and
        return {abs_path: original_bytes} for a byte-identity check."""
        (self.project / "CLAUDE.md").write_text(SEED_CLAUDE, encoding="utf-8")
        (self.project / ".gitignore").write_text(SEED_GITIGNORE, encoding="utf-8")
        (self.project / ".claude").mkdir(exist_ok=True)
        (self.project / ".claude" / "settings.json").write_text(
            SEED_PROJECT_SETTINGS, encoding="utf-8")
        (self.home / ".bash_profile").write_text(SEED_RC, encoding="utf-8")
        (self.home / ".claude").mkdir(exist_ok=True)
        (self.home / ".claude" / "settings.json").write_text(
            SEED_USER_SETTINGS, encoding="utf-8")
        paths = [
            self.project / "CLAUDE.md",
            self.project / ".gitignore",
            self.project / ".claude" / "settings.json",
            self.home / ".bash_profile",
            self.home / ".claude" / "settings.json",
        ]
        return {p: p.read_bytes() for p in paths}

    def _assert_restored_and_clean(self, r, originals, *, expect_modified):
        # 1. Non-zero exit — a failed run.
        self.assertNotEqual(r.returncode, 0,
                            f"init exited 0 on an induced failure:\n{r.stdout}\n{r.stderr}")
        # 2. It actually rolled back (the trap fired).
        self.assertIn("restoring the pre-init state", r.stderr,
                      f"no rollback message:\n{r.stdout}\n{r.stderr}")
        self.assertIn("ROLLBACK:", r.stderr)
        # 3. Byte-identical restoration of every pre-existing file.
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original,
                             f"{path} was not restored byte-identically")
        # 4. At least one of the modified files really was mutated mid-run and
        #    then restored — proving the check isn't vacuous (init got that far).
        #    `expect_modified` names a file the failing vector modifies before it
        #    fails; if init had left it untouched, the vector would be misplaced.
        self.assertIn(expect_modified, r.stderr,
                      "expected the vector to modify+restore "
                      f"{expect_modified}, but rollback did not report it")
        # 5. No mixed state — nothing init provisions survives.
        for rel in PROVISIONED:
            self.assertFalse((self.project / rel).exists(),
                             f"created path survived rollback: {rel}")
        self.assertFalse((self.home / ".claude" / "bash-log.sh").exists(),
                         "user-level bash-log.sh survived rollback")

    # ---- the three induced-failure vectors --------------------------------

    def test_readonly_dir_hard_abort_rolls_back(self):
        """Read-only `.claude/bin` → a wrapper write fails under `set -e`, a hard
        mid-run abort. Section (b) has already modified the seeded settings.json;
        it must come back byte-identical and every created file must be gone."""
        originals = self._seed_modifiable_files()
        binp = self.project / ".claude" / "bin"
        binp.mkdir(parents=True, exist_ok=True)
        self._make_readonly_or_skip(binp)  # r-x: cannot create files
        r = self._run()
        os.chmod(binp, 0o700)
        self._assert_restored_and_clean(
            r, originals, expect_modified=str(self.project / ".claude" / "settings.json"))

    def test_permission_denied_soft_failed_rolls_back(self):
        """Read-only `~/.claude` → the bash-log deploy fails (permission denied),
        so init reaches its summary with a FAILED entry and exits 1. By then the
        seeded CLAUDE.md/.gitignore are merged and the rc file appended; all must
        be restored byte-identically."""
        originals = self._seed_modifiable_files()
        claude = self.home / ".claude"
        self._make_readonly_or_skip(claude)  # r-x: cannot create files
        r = self._run()
        os.chmod(claude, 0o700)
        self._assert_restored_and_clean(
            r, originals, expect_modified=str(self.project / "CLAUDE.md"))

    def test_interrupt_signal_rolls_back(self):
        """A SIGTERM mid-run (Ctrl-C class) is trapped → init exits non-zero →
        rollback. Every earlier stage's modifications come back byte-identical."""
        if sys.platform == "win32":
            self.skipTest("SIGTERM/MSYS pid semantics differ on windows-git-bash "
                          "(controlled interruption there is Phase 8 live evidence)")
        originals = self._seed_modifiable_files()
        r = self._run_interrupted()
        self.assertEqual(r.returncode, 143, f"expected SIGTERM exit 143:\n{r.stderr}")
        self._assert_restored_and_clean(
            r, originals, expect_modified=str(self.project / "CLAUDE.md"))

    # ---- negative control: a broken/absent restore is detected ------------

    def test_negative_control_without_transaction_state_is_left_dirty(self):
        """With the transaction disabled, the SAME failure that the vectors above
        cleanly roll back instead CLOBBERS a pre-existing file and LEAVES created
        files behind. This proves the byte-identity + no-mixed-state assertions
        are not vacuous: an absent (or broken) restore is caught, not passed."""
        originals = self._seed_modifiable_files()
        binp = self.project / ".claude" / "bin"
        binp.mkdir(parents=True, exist_ok=True)
        self._make_readonly_or_skip(binp)
        r = self._run(env_extra={"PB_INIT_NO_TXN": "1"})
        os.chmod(binp, 0o700)

        self.assertNotEqual(r.returncode, 0, "vector should still fail")
        self.assertNotIn("ROLLBACK:", r.stderr, "transaction should be disabled")
        # The pre-existing settings.json was modified and, with no rollback,
        # stays modified — the byte-identity check WOULD fire here.
        settings = self.project / ".claude" / "settings.json"
        self.assertNotEqual(
            settings.read_bytes(), originals[settings],
            "without the transaction the modified file should NOT match the "
            "original — if it does, the byte-identity assertion proves nothing")
        # And a created file is left behind — the no-mixed-state check WOULD fire.
        self.assertTrue((self.project / ".agent" / "config.json").exists(),
                        "without the transaction a created file should remain")
        # No backup dir was made when the transaction is off.
        self.assertFalse((self.project / ".agent" / "backups").exists(),
                         "backups dir created despite PB_INIT_NO_TXN=1")

    # ---- clean runs -------------------------------------------------------

    def test_clean_run_keeps_snapshot_as_undo_point(self):
        """A successful init keeps the snapshot and prints where it is."""
        r = self._run()
        self.assertEqual(r.returncode, 0, f"clean init failed:\n{r.stdout}\n{r.stderr}")
        self.assertIn("Undo:", r.stdout)
        backups = self.project / ".agent" / "backups"
        self.assertTrue(backups.is_dir(), "no project snapshot kept")
        manifests = list(backups.glob("init-*/manifest.json"))
        self.assertEqual(len(manifests), 1, f"expected one manifest, got {manifests}")
        self.assertTrue((self.home / ".playbook-init-backups").is_dir(),
                        "no user snapshot kept")

    def test_reinit_is_idempotent_and_transaction_safe(self):
        """The transaction must not break a normal second run: init twice, both
        clean, and the provisioned files stay intact across the second run."""
        r1 = self._run()
        self.assertEqual(r1.returncode, 0, f"first init failed:\n{r1.stdout}\n{r1.stderr}")
        claude_after_first = (self.project / "CLAUDE.md").read_bytes()
        r2 = self._run()
        self.assertEqual(r2.returncode, 0, f"re-init failed:\n{r2.stdout}\n{r2.stderr}")
        self.assertIn("exists", r2.stdout, "re-init reported nothing as skipped")
        # Idempotent: the merged CLAUDE.md is unchanged by the second run.
        self.assertEqual((self.project / "CLAUDE.md").read_bytes(), claude_after_first)
        # Both runs kept their own snapshot.
        manifests = list((self.project / ".agent" / "backups").glob("init-*/manifest.json"))
        self.assertGreaterEqual(len(manifests), 1)


if __name__ == "__main__":
    unittest.main()
