#!/usr/bin/env python3
"""The declared Python floor is 3.10 EVERYWHERE (owner decision).

Three surfaces used to disagree: `tasks doctor` accepted >= 3.8, the docs and CI
said 3.10, and `scripts/tasks` already refused < 3.10. This module pins the
reconciled contract:

  1. doctor's floor verdict REJECTS a 3.8 / 3.9 interpreter report and accepts
     3.10+ (unit, on the pure `_python_floor_verdict` seam so the running
     interpreter — which is always >= 3.10 here — need not be downgraded);
  2. every shell entrypoint that execs python3 fails with a clear
     "needs python3 >= 3.10" message on a < 3.10 interpreter instead of a bare
     parser traceback (integration, via a fake `python3` shim reporting 3.9);
     the ADVISORY command-guard-hook fails OPEN loudly and the codex hooks keep
     their per-event allow/block polarity;
  3. the codex python hooks (which import provider modules that use 3.10-only
     `match` syntax) gate BEFORE that import so a < 3.10 interpreter gets the
     clear message, not a SyntaxError repr.

The full min/latest interpreter matrix across linux/macos/windows is Phase-8
live-platform work (PB-PYTHON-FLOOR); these are the on-this-platform proofs.

Run: python3 -m unittest tests.test_python_floor
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins" / "playbook"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(PLUGIN))

from tests._bashcheck import bash_or_skip  # noqa: E402
from tests._oldpython import make_oldpython_path  # noqa: E402

FLOOR_MSG = "python3 >= 3.10"


# --------------------------------------------------------------------------- #
# 1. doctor — the floor verdict itself (pure, testable seam).
# --------------------------------------------------------------------------- #
class DoctorFloorVerdict(unittest.TestCase):
    """doctor must REJECT < 3.10 and ACCEPT >= 3.10 (was: accepted >= 3.8)."""

    def setUp(self):
        from tasks.diagnostics import _python_floor_verdict
        self.verdict = _python_floor_verdict

    def test_rejects_3_8(self):
        ok, _ = self.verdict(3, 8)
        self.assertFalse(ok, "doctor accepted Python 3.8 — below the 3.10 floor")

    def test_rejects_3_9(self):
        ok, _ = self.verdict(3, 9)
        self.assertFalse(ok, "doctor accepted Python 3.9 — below the 3.10 floor")

    def test_accepts_3_10(self):
        ok, _ = self.verdict(3, 10)
        self.assertTrue(ok, "doctor rejected Python 3.10 — the declared floor")

    # negative control: the fix must not over-reject supported interpreters.
    def test_accepts_newer(self):
        for major, minor in ((3, 11), (3, 13), (4, 0)):
            ok, _ = self.verdict(major, minor)
            self.assertTrue(ok, f"doctor rejected supported Python {major}.{minor}")

    def test_detail_names_the_floor_on_rejection(self):
        _, detail = self.verdict(3, 9)
        self.assertIn("3.10", detail,
                      f"rejection detail must name the floor, got {detail!r}")


# --------------------------------------------------------------------------- #
# 2. codex python hooks — gate BEFORE the 3.10-only provider import, keeping
#    each hook's documented allow/block polarity.
# --------------------------------------------------------------------------- #
def _load_hook(name: str):
    """Import a codex hook script (hyphenated, non-package) as a module so its
    module-level floor helper can be called directly. main() is guarded by
    __name__ == '__main__' and does not run on import."""
    path = SCRIPTS / name
    modname = name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CodexApplyPatchFloorGate(unittest.TestCase):
    def setUp(self):
        self.mod = _load_hook("codex-apply-patch-hook")

    def test_pretooluse_fails_closed_below_floor(self):
        # Enforcing deny-without-task read: a guard that cannot run must NOT
        # silently allow the edit — polarity is preserved (exit 2 = block).
        rc = self.mod._floor_exit((3, 9, 0), "PreToolUse")
        self.assertEqual(rc, 2, "apply_patch PreToolUse must fail CLOSED below 3.10")

    def test_posttooluse_fails_open_below_floor(self):
        rc = self.mod._floor_exit((3, 9, 0), "PostToolUse")
        self.assertEqual(rc, 0, "apply_patch PostToolUse must fail OPEN below 3.10")

    # negative control: a supported interpreter is not gated at all.
    def test_supported_interpreter_not_gated(self):
        self.assertIsNone(self.mod._floor_exit((3, 10, 0), "PreToolUse"))
        self.assertIsNone(self.mod._floor_exit((3, 12, 0), "PostToolUse"))


class CodexAdvisoryFloorGate(unittest.TestCase):
    """stop / user-prompt hooks are advisory — always fail OPEN below the floor."""

    def test_stop_hook_gate(self):
        mod = _load_hook("codex-stop-hook")
        self.assertFalse(mod._floor_ok((3, 9, 0)))
        self.assertTrue(mod._floor_ok((3, 10, 0)))   # negative control

    def test_user_prompt_hook_gate(self):
        mod = _load_hook("codex-user-prompt-hook")
        self.assertFalse(mod._floor_ok((3, 9, 0)))
        self.assertTrue(mod._floor_ok((3, 11, 0)))   # negative control


# --------------------------------------------------------------------------- #
# 3. shell entrypoints — clear message on a < 3.10 interpreter (integration).
# --------------------------------------------------------------------------- #
def _copy_scripts(dest: Path) -> Path:
    shutil.copytree(SCRIPTS, dest)
    return dest


class ShellEntrypointFloorGuard(unittest.TestCase):
    """Run each entrypoint with a fake python3 that reports 3.9 and assert the
    clear floor message. Hermetic: a temp $HOME and a temp copy of scripts/ so
    an unfixed (red) run cannot touch real user config."""

    def setUp(self):
        self.bash = bash_or_skip()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.oldpath = make_oldpython_path(self.tmp / "oldbin")
        self.scr = _copy_scripts(self.tmp / "scripts")
        self.proj = self.tmp / "proj"
        self.proj.mkdir()

    def _run(self, script_name: str, *, args=(), stdin=b"", cwd=None):
        env = dict(os.environ)
        env["PATH"] = self.oldpath          # fake python3 (reports 3.9) wins
        env["HOME"] = str(self.tmp / "home")
        env.pop("PLAYBOOK_ROLE", None)
        (self.tmp / "home").mkdir(exist_ok=True)
        return subprocess.run(
            [self.bash, str(self.scr / script_name), *args],
            input=stdin, cwd=str(cwd or self.proj), env=env,
            capture_output=True, timeout=60)

    # -- fail-closed launchers/CLIs: non-zero + the floor message on stderr. --
    def _assert_fail_closed(self, name, **kw):
        r = self._run(name, **kw)
        err = r.stderr.decode("utf-8", "replace")
        self.assertNotEqual(r.returncode, 0, f"{name} did not refuse < 3.10: {err!r}")
        self.assertIn(FLOOR_MSG, err, f"{name} gave no clear floor message: {err!r}")

    def test_tasks(self):
        self._assert_fail_closed("tasks", args=("doctor",))

    def test_sandbox(self):
        self._assert_fail_closed("sandbox", args=("--list-agents",))

    def test_init(self):
        self._assert_fail_closed("init")

    def test_playbook_agy(self):
        self._assert_fail_closed("playbook-agy")

    def test_playbook_codex(self):
        self._assert_fail_closed("playbook-codex")

    def test_playbook_grok(self):
        self._assert_fail_closed("playbook-grok")

    def test_playbook_pi(self):
        self._assert_fail_closed("playbook-pi")

    # -- ADVISORY hook: fails OPEN (exit 0) but warns LOUDLY about the floor. --
    def test_command_guard_hook_fails_open_loudly(self):
        payload = b'{"hook_event_name":"PreToolUse","tool_name":"Bash",' \
                  b'"tool_input":{"command":"ls -la"}}'
        r = self._run("command-guard-hook", stdin=payload)
        err = r.stderr.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0,
                         f"advisory command-guard-hook must fail OPEN on < 3.10: {err!r}")
        self.assertIn(FLOOR_MSG, err,
                      f"fail-open was silent about the floor: {err!r}")

    # -- negative control: on the REAL (>= 3.10) interpreter the guard does NOT
    #    fire — no floor message, safe command still allowed (exit 0). Proves the
    #    guard is a floor check, not a blanket refusal. --
    def test_supported_interpreter_passes_guard(self):
        env = dict(os.environ)
        env.pop("PLAYBOOK_ROLE", None)
        env["HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir(exist_ok=True)
        payload = b'{"hook_event_name":"PreToolUse","tool_name":"Bash",' \
                  b'"tool_input":{"command":"ls -la"}}'
        r = subprocess.run(
            [self.bash, str(self.scr / "command-guard-hook")],
            input=payload, cwd=str(self.proj), env=env,
            capture_output=True, timeout=60)
        err = r.stderr.decode("utf-8", "replace")
        self.assertNotIn(FLOOR_MSG, err,
                         f"floor guard false-fired on a supported interpreter: {err!r}")
        self.assertEqual(r.returncode, 0,
                         f"safe command blocked on a supported interpreter: {err!r}")


if __name__ == "__main__":
    unittest.main()
