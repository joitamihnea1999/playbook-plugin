#!/usr/bin/env python3
"""Doctor must inspect the install it is RUNNING FROM, not a stray cache (F16).

Field evidence (StrataDB batch 4, task 010): `tasks doctor` reported 4 FAIL
(state-echo-hook / task-gate-hook "missing", gate-text-truncation, session-id
resolver) + 8 WARN (quote-wrapped commands in a grok marketplace-cache copy)
while those same hooks demonstrably enforced the whole session. Root cause:

  * the hooks/resolver checks hunted `~/.claude/plugins/**` by mtime and never
    considered the tree the running module itself belongs to — the one place
    guaranteed to be the code that is executing (the version check was already
    fixed to do exactly this, task 010; the hook checks were missed);
  * `hooks_check_report` scanned every copy any host might load (correct — a
    stale grok copy WAS the firing one in the AloVet bug) but never said which
    copy is the one this CLI runs from, so stray-cache noise was
    indistinguishable from a defect in the live install.

"A health check that cries wolf while the system works trains you to ignore
it" — the journal's words. These tests pin the fix: two install copies
present, doctor reports on the bound one; stray-copy findings are labeled as
such; and a defect in the AUTHORITATIVE copy still warns at full volume (the
negative control — labeling must not soften the real regression).

Run: python3 -m unittest tests.test_doctor_binding
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

from tasks.hooks_check import EXPECTED_HOOKS, authoritative_hooks_path  # noqa: E402

STRAY_MARKER = "not the one this CLI runs from"


def make_project() -> Path:
    tmp = tempfile.mkdtemp()
    proj = Path(tmp) / "proj"
    (proj / ".agent" / "tasks" / "001-x").mkdir(parents=True)
    (proj / ".agent" / "tasks" / "001-x" / "task.md").write_text(
        "# 001 - X\n\n## Status\npending\n", encoding="utf-8")
    return proj


def make_home(*, stray_claude_scripts: bool = False,
              grok_quote_wrapped: bool = False) -> Path:
    """A controlled $HOME so the test never reads the developer's real caches."""
    home = Path(tempfile.mkdtemp()) / "home"
    home.mkdir(parents=True)
    if stray_claude_scripts:
        # An install-cache shell with NO hook scripts inside: the mtime-glob
        # bait. Before the fix, doctor picked this as "the" scripts dir and
        # reported every hook missing.
        (home / ".claude" / "plugins" / "cache" / "mp" / "playbook" / "scripts").mkdir(parents=True)
    if grok_quote_wrapped:
        hooks_dir = home / ".grok" / "marketplace-cache" / "old" / "plugins" / "playbook" / "hooks"
        hooks_dir.mkdir(parents=True)
        obj = {"hooks": {
            ev: [{"hooks": [{"type": "command",
                             "command": f'"${{CLAUDE_PLUGIN_ROOT}}/scripts/{s}"'}]} for s in scripts]
            for ev, scripts in EXPECTED_HOOKS.items()
        }}
        (hooks_dir / "hooks.json").write_text(json.dumps(obj), encoding="utf-8")
    return home


def run_doctor(proj: Path, home: Path, extra_env: dict | None = None) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PLUGIN)
    env["HOME"] = str(home)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, "-m", "tasks.cli", "doctor"],
                       cwd=proj, env=env, capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


class DoctorInspectsTheRunningInstall(unittest.TestCase):
    def test_hook_checks_resolve_the_running_tree_not_a_stray_cache(self):
        # Two copies present: the running tree (this repo, hooks intact) and a
        # newer-mtime stray cache with no hooks. Doctor must report on the one
        # it runs from.
        out = run_doctor(make_project(), make_home(stray_claude_scripts=True))
        self.assertIn("[PASS] hooks: state-echo-hook", out)
        self.assertIn("[PASS] hooks: task-gate-hook", out)
        self.assertIn("[PASS] hooks: gate text truncation", out)
        self.assertNotIn("gate-echo-lib.sh not found", out)

    def test_stray_copy_findings_are_labeled_as_not_the_running_install(self):
        out = run_doctor(make_project(), make_home(grok_quote_wrapped=True))
        # The stray grok cache's quote-wrap findings surface — but labeled.
        stray_lines = [l for l in out.splitlines()
                       if "marketplace-cache" in l and "quote-wrapped" in l]
        self.assertTrue(stray_lines, f"stray copy findings missing:\n{out}")
        for line in stray_lines:
            self.assertIn(STRAY_MARKER, line)
        # And the copy this CLI runs from reports no quote-wrap defect.
        own_hooks = str((PLUGIN / "hooks" / "hooks.json").resolve())
        self.assertFalse(
            [l for l in out.splitlines() if own_hooks in l and "quote-wrapped" in l],
            "the running install was reported quote-wrapped")

    def test_defect_in_the_authoritative_copy_still_warns_at_full_volume(self):
        # Negative control: labeling stray copies must not soften a REAL
        # regression in the copy the host actually loads (the AloVet bug).
        fixture = Path(tempfile.mkdtemp()) / "plugroot"
        (fixture / "scripts").mkdir(parents=True)
        for scripts in EXPECTED_HOOKS.values():
            for script in scripts:
                s = fixture / "scripts" / script
                s.write_text("#!/bin/bash\n# GATE_TEXT_STORE marker\n", encoding="utf-8")
                s.chmod(0o755)
        (fixture / "hooks").mkdir()
        obj = {"hooks": {
            ev: [{"hooks": [{"type": "command",
                             "command": f'"${{CLAUDE_PLUGIN_ROOT}}/scripts/{s}"'}]} for s in scripts]
            for ev, scripts in EXPECTED_HOOKS.items()
        }}
        (fixture / "hooks" / "hooks.json").write_text(json.dumps(obj), encoding="utf-8")

        out = run_doctor(make_project(), make_home(),
                         extra_env={"CLAUDE_PLUGIN_ROOT": str(fixture)})
        auth_lines = [l for l in out.splitlines()
                      if str(fixture) in l and "quote-wrapped" in l]
        self.assertTrue(auth_lines, f"authoritative defect not reported:\n{out}")
        for line in auth_lines:
            self.assertNotIn(STRAY_MARKER, line,
                             "a defect in the BOUND copy was softened as stray")


class AuthoritativePathResolution(unittest.TestCase):
    def test_env_plugin_root_wins_when_set(self):
        fixture = Path(tempfile.mkdtemp())
        (fixture / "hooks").mkdir(parents=True)
        (fixture / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
        p = authoritative_hooks_path(env={"CLAUDE_PLUGIN_ROOT": str(fixture)})
        self.assertEqual(p.resolve(), (fixture / "hooks" / "hooks.json").resolve())

    def test_falls_back_to_the_running_modules_own_tree(self):
        p = authoritative_hooks_path(env={})
        self.assertIsNotNone(p)
        self.assertEqual(p.resolve(), (PLUGIN / "hooks" / "hooks.json").resolve())


if __name__ == "__main__":
    unittest.main()
