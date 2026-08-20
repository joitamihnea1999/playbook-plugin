#!/usr/bin/env python3
"""Point tests for hook-command quoting validation (task 019).

Guards the field bug (AloVet 2026-07-20): quote-wrapped hooks.json commands
fail-open on grok. Covers the shipped file (must be clean and dual-host form),
the pure validator's every branch (full-wrap flagged, bash-wrapped clean,
empty/non-string flagged, missing→[], malformed→advisory, shape checks), and
the doctor §1f wiring seam (buggy fixture → warnings, clean fixture → silent).

Pure stdlib unittest (no hypothesis — honors the stdlib-only runtime invariant).
Run: python3 tests/test_hooks_json_quoting.py   (or: python3 -m unittest ...)
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

# The runtime tree is plugins/playbook/ (dispatcher sets PYTHONPATH there).
_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(_PLUGIN))

from tasks.hooks_check import (  # noqa: E402
    EXPECTED_HOOKS,
    _installed_playbook_paths,
    candidate_hooks_paths,
    hook_command_issues,
    hooks_check_report,
)

SHIPPED = _PLUGIN / "hooks" / "hooks.json"


def _write_plugin_tree(root: Path, commands: dict) -> Path:
    """Lay down a minimal plugin tree (hooks/hooks.json + scripts/) and return
    the hooks.json path. `commands` maps event name -> command string."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for scripts in EXPECTED_HOOKS.values():
        for script in scripts:
            (root / "scripts" / script).write_text("#!/bin/bash\n", encoding="utf-8")
    hooks_dir = root / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    obj = {"hooks": {}}
    for event, cmd in commands.items():
        cmds = cmd if isinstance(cmd, list) else [cmd]
        obj["hooks"][event] = [{"hooks": [{"type": "command", "command": c}]} for c in cmds]
    path = hooks_dir / "hooks.json"
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return path


def _good_commands() -> dict:
    return {
        ev: [f'bash "${{CLAUDE_PLUGIN_ROOT}}/scripts/{s}"' for s in scripts]
        for ev, scripts in EXPECTED_HOOKS.items()
    }


class ShippedFileTests(unittest.TestCase):
    def test_shipped_hooks_json_is_clean(self):
        self.assertEqual(hook_command_issues(SHIPPED), [])

    def test_shipped_commands_are_dual_host_form(self):
        data = json.loads(SHIPPED.read_text(encoding="utf-8"))
        cmds = [
            h["command"]
            for entries in data["hooks"].values()
            for e in entries
            for h in e["hooks"]
        ]
        self.assertEqual(len(cmds), sum(len(v) for v in EXPECTED_HOOKS.values()))
        for c in cmds:
            self.assertTrue(c.startswith('bash "'), c)
            self.assertTrue(c.endswith('"'), c)
            # not a matched full-wrap
            self.assertNotEqual(c[0], c[-1])

    def test_shipped_scripts_exist_and_executable(self):
        import os

        scripts_dir = SHIPPED.parent.parent / "scripts"
        for scripts in EXPECTED_HOOKS.values():
            for script in scripts:
                p = scripts_dir / script
                self.assertTrue(p.exists(), f"{script} missing")
                self.assertTrue(os.access(p, os.X_OK), f"{script} not executable")


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bash_wrapped_form_is_clean(self):
        path = _write_plugin_tree(self.root, _good_commands())
        self.assertEqual(hook_command_issues(path), [])

    def test_full_wrap_double_quote_flagged(self):
        cmds = _good_commands()
        cmds["PreToolUse"] = '"${CLAUDE_PLUGIN_ROOT}/scripts/task-gate-hook"'
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("quote-wrapped" in i and "PreToolUse" in i for i in issues), issues)

    def test_full_wrap_single_quote_flagged(self):
        cmds = _good_commands()
        cmds["Stop"] = "'${CLAUDE_PLUGIN_ROOT}/scripts/stop-hook'"
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("quote-wrapped" in i and "Stop" in i for i in issues), issues)

    def test_leading_whitespace_then_wrapped_flagged(self):
        cmds = _good_commands()
        cmds["PostToolUse"] = '  "${CLAUDE_PLUGIN_ROOT}/scripts/state-echo-hook"  '
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("quote-wrapped" in i and "PostToolUse" in i for i in issues), issues)

    def test_bare_form_is_clean_of_quote_defect(self):
        # Bare (no bash, no quotes) is the reporter's own workaround — it is
        # NOT quote-wrapped, so the quoting check must not flag it.
        cmds = {
            ev: [f"${{CLAUDE_PLUGIN_ROOT}}/scripts/{s}" for s in scripts]
            for ev, scripts in EXPECTED_HOOKS.items()
        }
        path = _write_plugin_tree(self.root, cmds)
        self.assertEqual(
            [i for i in hook_command_issues(path) if "quote-wrapped" in i], []
        )

    def test_empty_command_flagged(self):
        cmds = _good_commands()
        cmds["Stop"] = "   "
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("empty" in i for i in issues), issues)

    def test_non_string_command_flagged(self):
        path = _write_plugin_tree(self.root, _good_commands())
        obj = json.loads(path.read_text())
        obj["hooks"]["Stop"][0]["hooks"][0]["command"] = 123
        path.write_text(json.dumps(obj))
        issues = hook_command_issues(path)
        self.assertTrue(any("not a string" in i for i in issues), issues)

    def test_missing_file_is_silent(self):
        self.assertEqual(hook_command_issues(self.root / "nope.json"), [])

    def test_malformed_json_is_single_advisory(self):
        bad = self.root / "bad.json"
        bad.write_text("{not valid", encoding="utf-8")
        issues = hook_command_issues(bad)
        self.assertEqual(len(issues), 1)
        self.assertIn("JSON", issues[0])

    def test_missing_registration_flagged(self):
        cmds = _good_commands()
        del cmds["SessionEnd"]
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("SessionEnd" in i and "missing" in i for i in issues), issues)

    def test_missing_referenced_script_flagged(self):
        path = _write_plugin_tree(self.root, _good_commands())
        (self.root / "scripts" / "task-gate-hook").unlink()
        issues = hook_command_issues(path)
        self.assertTrue(any("task-gate-hook" in i and "not found" in i for i in issues), issues)

    def test_wrong_script_referenced_flagged(self):
        cmds = _good_commands()
        # PreToolUse points at the wrong script basename
        cmds["PreToolUse"] = 'bash "${CLAUDE_PLUGIN_ROOT}/scripts/session-start-hook"'
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(
            any("PreToolUse" in i and "task-gate-hook" in i for i in issues), issues
        )


class CandidatePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_env_plugin_root_included_when_present(self):
        path = _write_plugin_tree(self.root, _good_commands())
        env = {"CLAUDE_PLUGIN_ROOT": str(self.root)}
        paths = candidate_hooks_paths(project_path=None, env=env)
        self.assertIn(path.resolve(), [p.resolve() for p in paths])

    def test_installed_playbook_paths_reads_manifest(self):
        # A stale cache copy sits at <installPath>/hooks/hooks.json with a
        # version segment; installPath resolution (not a **/playbook/hooks glob)
        # is what reaches it. Fixture a minimal installed_plugins.json.
        home = self.root
        plugdir = home / ".claude" / "plugins"
        plugdir.mkdir(parents=True)
        install_path = plugdir / "cache" / "mp" / "playbook" / "9.9.9"
        (install_path).mkdir(parents=True)
        (plugdir / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "playbook@mp": [
                            {"scope": "user", "installPath": str(install_path), "version": "9.9.9"}
                        ],
                        "other-plugin@mp": [{"installPath": "/somewhere/else"}],
                    }
                }
            ),
            encoding="utf-8",
        )
        paths = _installed_playbook_paths(home)
        self.assertIn(install_path, paths)
        self.assertNotIn(Path("/somewhere/else"), paths)

    def test_installed_playbook_paths_soft_on_missing_manifest(self):
        self.assertEqual(_installed_playbook_paths(self.root), [])

    def test_nonexistent_candidates_dropped_and_deduped(self):
        # No env root, no workspace copy → only real files (the module-relative
        # shipped copy) survive; and it appears at most once.
        paths = candidate_hooks_paths(project_path="/nonexistent-xyz", env={})
        resolved = [p.resolve() for p in paths]
        self.assertEqual(len(resolved), len(set(resolved)))
        for p in paths:
            self.assertTrue(p.is_file())


class DoctorWiringTests(unittest.TestCase):
    """§1f seam: hooks_check_report drives the doctor warn() loop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_buggy_copy_produces_warnings(self):
        cmds = _good_commands()
        cmds["PreToolUse"] = '"${CLAUDE_PLUGIN_ROOT}/scripts/task-gate-hook"'
        _write_plugin_tree(self.root, cmds)
        env = {"CLAUDE_PLUGIN_ROOT": str(self.root)}
        report = hooks_check_report(project_path=None, env=env)
        self.assertTrue(report)
        label, detail = report[0]
        self.assertIn("hooks:", label)
        self.assertIn("quote-wrapped", detail)

    def test_clean_copy_is_silent(self):
        _write_plugin_tree(self.root, _good_commands())
        env = {"CLAUDE_PLUGIN_ROOT": str(self.root)}
        # Point project_path at an empty dir so only the env copy is scanned.
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        report = [
            r
            for r in hooks_check_report(project_path=str(empty), env=env)
            if str(self.root) in r[0]
        ]
        self.assertEqual(report, [])


class StaleForeignCopyCollapse(unittest.TestCase):
    """An older install is one row without hiding any of its findings.

    Field finding (1.5.31 audit): on a real machine, 6 of the doctor's 12
    warnings came from a `~/.grok/marketplace-cache/…` copy of v1.4.3 abandoned
    weeks earlier. Every one was correct and every one was noise — a cache from
    before `command-guard-hook` existed necessarily fails the check for it.
    Warning fatigue is how the findings that matter get skimmed past, so a
    older foreign copy groups into one row. It is NOT blanket: unknown, newer,
    or semantically same versions may be live and stay enumerated. The grouped
    row itself carries every finding, because an older copy may still be live.

    The `project_path` candidate (<root>/plugins/playbook/hooks/hooks.json) is
    the seam used to plant a foreign copy — a real scan source, not a test hook.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = self.root / "live"
        self.foreign = self.root / "plugins" / "playbook"
        _write_plugin_tree(self.auth, _good_commands())
        bad = _good_commands()
        bad["PreToolUse"] = '"${CLAUDE_PLUGIN_ROOT}/scripts/task-gate-hook"'
        bad["Stop"] = '"${CLAUDE_PLUGIN_ROOT}/scripts/stop-hook"'
        _write_plugin_tree(self.foreign, bad)

    def _stamp(self, root: Path, version: str):
        d = root / ".claude-plugin"
        d.mkdir(parents=True, exist_ok=True)
        (d / "plugin.json").write_text(
            json.dumps({"name": "playbook", "version": version}), encoding="utf-8")

    def _foreign_rows(self, verbose=False):
        env = {"CLAUDE_PLUGIN_ROOT": str(self.auth)}
        report = hooks_check_report(project_path=str(self.root), env=env,
                                    verbose=verbose)
        return [r for r in report if str(self.foreign) in r[0] + r[1]]

    def test_the_foreign_copy_is_actually_scanned(self):
        """Guard the seam itself: if the scanner stopped reading project_path,
        every assertion below would pass vacuously."""
        self._stamp(self.foreign, "1.4.3")
        self.assertTrue(self._foreign_rows(verbose=True),
                        "the planted foreign copy was never scanned")

    def test_older_version_copy_collapses_to_one_row(self):
        self._stamp(self.foreign, "1.4.3")
        rows = self._foreign_rows()
        self.assertEqual(len(rows), 1, f"not collapsed: {rows}")
        label, detail = rows[0]
        self.assertIn("older install copy", label)
        self.assertIn("1.4.3", detail, "the collapsed row must name the version")
        self.assertIn("--verbose", detail, "it must say how to see the rest")
        self.assertRegex(detail, r"\d+ issue\(s\)")
        self.assertIn("command-guard-hook", detail,
                      "grouping hid the missing enforcing hook")
        self.assertIn("Stop", detail, "grouping hid a later finding")

    def test_verbose_enumerates_them_again(self):
        self._stamp(self.foreign, "1.4.3")
        self.assertGreater(len(self._foreign_rows(verbose=True)), 1,
                           "--verbose must not hide anything")

    def test_unreadable_version_is_enumerated_not_assumed_stale(self):
        """No manifest proves neither age nor liveness."""
        rows = self._foreign_rows()
        self.assertGreater(len(rows), 1, f"unknown version was hidden: {rows}")

    def test_same_version_copy_is_still_enumerated(self):
        """The safety half: a foreign copy at the running version could be a
        genuinely live second install, so its findings stay individually visible.
        """
        from tasks.hooks_check import _code_version
        code_v = _code_version()
        self.assertTrue(code_v, "the running tree must have a readable version")
        self._stamp(self.foreign, code_v)
        self.assertGreater(len(self._foreign_rows()), 1,
                           "a same-version copy was wrongly collapsed")

    def test_equivalent_version_formatting_is_still_same_version(self):
        from tasks.hooks_check import _code_version
        code_v = _code_version()
        # Derived, never a literal: a hardcoded "01.5.33" stops being an
        # equivalent formatting of the running version the moment the version
        # is bumped, and this arm then asserts the opposite of its own name.
        for shown in (f"v{code_v}", f"0{code_v}"):
            with self.subTest(version=shown):
                self._stamp(self.foreign, shown)
                self.assertGreater(len(self._foreign_rows()), 1)

    def test_malformed_or_newer_version_is_not_called_stale(self):
        for shown in ("banana", "99.0.0"):
            with self.subTest(version=shown):
                self._stamp(self.foreign, shown)
                self.assertGreater(len(self._foreign_rows()), 1)

    def test_a_clean_stale_copy_says_nothing_at_all(self):
        """Negative control: collapsing must not invent a row for a copy that
        has no findings."""
        _write_plugin_tree(self.foreign, _good_commands())
        self._stamp(self.foreign, "1.0.0")
        self.assertEqual(self._foreign_rows(), [])


if __name__ == "__main__":
    unittest.main()
