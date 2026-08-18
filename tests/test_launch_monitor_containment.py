#!/usr/bin/env python3
"""launch-monitor is platform-portable via provider.sandbox (batch-5 fix).

Field finding (StrataDB batch 5, owner-hit): launch-monitor:175 hard-exec'd
`sandbox-exec -p ...` — macOS seatbelt with no Linux branch — so the monitor
could never run on Linux even though provider.sandbox has carried a proven
bwrap path since the 1.5.3 spike. The fix: the launcher delegates containment
to provider.sandbox (single source of truth, bind-order lesson included) with
the project read-only and the monitor dir as the only project-side writable.

Covers:
  * `--ro-project` in provider.sandbox main() — the project binds read-only,
    the --rw monitor dir binds AFTER it (stays writable inside the ro project);
  * `--print-argv` — the wrapped argv is inspectable without executing;
  * the launcher script itself: delegates, no hardcoded sandbox-exec, and
    uses --safe-mode (T136: a `--settings {}` override cannot suppress
    plugin-registered hooks).

Run: python3 -m unittest tests.test_launch_monitor_containment
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
LAUNCHER = PLUGIN / "scripts" / "monitor-lib" / "launch-monitor"


def _clean_env() -> dict:
    # PLAYBOOK_SANDBOXED would short-circuit wrapping (nesting guard).
    env = {k: v for k, v in os.environ.items() if k != "PLAYBOOK_SANDBOXED"}
    env["PYTHONPATH"] = str(PLUGIN)
    return env


def _print_argv(project: Path, *flags: str) -> list[str]:
    r = subprocess.run(
        [sys.executable, "-m", "provider.sandbox", *flags,
         "--project-root", str(project), "--print-argv", "--", "echo", "hi"],
        env=_clean_env(), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(f"print-argv failed rc={r.returncode}: {r.stderr}")
    return [l for l in r.stdout.splitlines() if l]


def bind_index(argv: list[str], path: str, kinds=("--bind", "--ro-bind")) -> int:
    for i in range(len(argv) - 2):
        if argv[i] in kinds and argv[i + 1] == path:
            return i
    return -1


@unittest.skipUnless(shutil.which("bwrap"), "bwrap not installed")
class RoProjectArgvShape(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve()
        self.mdir = self.proj / ".agent" / "monitor"
        self.mdir.mkdir(parents=True)

    def test_ro_project_binds_project_read_only(self):
        argv = _print_argv(self.proj, "--ro-project", "--rw", str(self.mdir),
                           "--agent", "claude")
        i = bind_index(argv, str(self.proj))
        self.assertNotEqual(i, -1, f"project not bound at all: {argv}")
        self.assertEqual(argv[i], "--ro-bind",
                         "--ro-project must bind the project read-only")

    def test_monitor_dir_binds_writable_after_project(self):
        argv = _print_argv(self.proj, "--ro-project", "--rw", str(self.mdir),
                           "--agent", "claude")
        ip = bind_index(argv, str(self.proj))
        im = bind_index(argv, str(self.mdir), kinds=("--bind",))
        self.assertNotEqual(im, -1, f"monitor dir not rw-bound: {argv}")
        self.assertGreater(im, ip,
                           "monitor dir must bind AFTER the project so it "
                           "stays writable inside the read-only project")

    def test_without_flag_project_stays_writable(self):
        # Negative control: --ro-project must be opt-in, not a default flip.
        argv = _print_argv(self.proj, "--agent", "claude")
        i = bind_index(argv, str(self.proj))
        self.assertEqual(argv[i], "--bind")


class LauncherDelegation(unittest.TestCase):
    def setUp(self):
        self.text = LAUNCHER.read_text(encoding="utf-8")

    def test_delegates_to_provider_sandbox(self):
        self.assertIn("provider.sandbox", self.text)
        self.assertIn("--ro-project", self.text)

    def test_no_hardcoded_seatbelt_exec(self):
        self.assertNotIn("exec sandbox-exec", self.text,
                         "platform containment must come from provider."
                         "sandbox, not a hardcoded macOS-only exec")

    def test_hooks_disabled_via_safe_mode(self):
        # T136: a --settings '{}' override cannot suppress plugin-registered
        # hooks; --safe-mode disables them at the source.
        self.assertIn("--safe-mode", self.text)


if __name__ == "__main__":
    unittest.main()
