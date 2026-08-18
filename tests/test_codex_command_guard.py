#!/usr/bin/env python3
"""Codex side of the destructive-command interlock (1.5.31).

Two things must hold for Codex parity: (1) `render_playbook_hooks` registers the
guard as a PreToolUse hook scoped to `^exec_command$` (Codex CAN pre-block exec,
it just didn't before); (2) `command_guard.classify_command` understands Codex's
`exec_command` shapes — a string, an argv LIST, and the `bash -lc "<script>"`
wrapper Codex uses — so the dangerous form is caught and the safe one is not.

Run: python3 tests/test_codex_command_guard.py
"""
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
sys.path.insert(0, str(_HERE.parent / "plugins/playbook/scripts"))
from provider.codex_hooks import render_playbook_hooks  # noqa: E402
import command_guard as cg  # noqa: E402


class CodexHookRegistration(unittest.TestCase):
    def test_exec_command_pretooluse_runs_the_guard(self):
        pre = render_playbook_hooks()["hooks"]["PreToolUse"]
        guard = [e for e in pre if e.get("matcher") == "^exec_command$"]
        self.assertEqual(len(guard), 1, "no ^exec_command$ PreToolUse guard for Codex")
        cmd = guard[0]["hooks"][0]["command"]
        self.assertIn("command_guard.py", cmd)
        # apply_patch task gate must still be present (unbroken).
        self.assertTrue(any(e.get("matcher") == "^apply_patch$" for e in pre))


class CodexExecShapes(unittest.TestCase):
    def test_string_form(self):
        self.assertEqual(cg.classify_command("git push --force")[0], "block")

    def test_argv_list_form(self):
        # Codex exec often delivers argv as a list.
        self.assertEqual(cg.classify_command(["rm", "-rf", "/"])[0], "block")

    def test_bash_lc_wrapper_is_unwrapped(self):
        # The dangerous command hides behind the interpreter token.
        self.assertEqual(cg.classify_command(["bash", "-lc", "rm -rf /"])[0], "block")
        self.assertEqual(cg.classify_command("bash -lc 'git push --force'")[0], "block")

    def test_safe_exec_forms_allow(self):
        for safe in ("npm run build", ["bash", "-lc", "pytest -q"],
                     ["rm", "-rf", "./build"], "git push"):
            self.assertEqual(cg.classify_command(safe)[0], "allow", f"false-positive: {safe!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
