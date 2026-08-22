"""NEW-1 (1.5.11 audit): the code-edit gate's .agent/.claude exemption must not
be defeated by `..` traversal.

The gate exempted any path CONTAINING `.agent`/`.claude` as a component, without
resolving `..`. So `.agent/../src/main.py` was exempted (exit 0) while the write
landed on the real code file `src/main.py` — a one-string bypass of the core
"no code without an active task" boundary. Reachable by any agent/prompt-injection
that can name a path.
"""

from __future__ import annotations

import json
import os
import subprocess
from tests._bashcheck import bash_or_skip
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "plugins" / "playbook" / "scripts" / "task-gate-hook"

import sys  # noqa: E402
sys.path.insert(0, str(REPO_ROOT / "plugins" / "playbook"))
from provider.policy import _is_management_path  # noqa: E402


class CodexManagementPathTraversal(unittest.TestCase):
    """NEW-1 codex twin: _is_management_path must resolve `..` before exempting."""

    def test_traversal_out_of_agent_is_not_management(self):
        self.assertFalse(_is_management_path("/proj/.agent/../src/main.py"))
        self.assertFalse(_is_management_path("/proj/.claude/../src/main.py"))

    def test_genuine_management_paths_still_true(self):
        self.assertTrue(_is_management_path("/proj/.agent/tasks/001-x/task.md"))
        self.assertTrue(_is_management_path("/proj/.claude/settings.json"))

    def test_plain_code_path_is_not_management(self):
        self.assertFalse(_is_management_path("/proj/src/main.py"))

    def test_deep_traversal_out_of_management_is_not_management(self):
        """`..` that starts INSIDE the management tree must still resolve out."""
        self.assertFalse(
            _is_management_path("/proj/.agent/tasks/../../src/main.py"))
        self.assertFalse(
            _is_management_path("/proj/x/.claude/../../src/a.py"))

    def test_backslash_traversal_is_resolved_too(self):
        """Backslashes normalize to `/` BEFORE `..` resolution, so a Windows-
        spelled traversal cannot dodge the normpath."""
        self.assertFalse(_is_management_path(".agent\\..\\src\\main.py"))

    def test_traversal_that_lands_back_inside_agent_is_management(self):
        # Negative control for the traversal rule: resolution, not the mere
        # presence of `..`, decides — this path genuinely ends under .agent/.
        self.assertTrue(_is_management_path("/proj/.agent/../.agent/x"))

    def test_lookalike_components_are_not_management(self):
        self.assertFalse(_is_management_path("/proj/.agentx/file.py"))
        self.assertFalse(_is_management_path("/proj/my.claude/file.py"))

    def test_unicode_management_path_is_management(self):
        self.assertTrue(
            _is_management_path("/proj/.agent/tasks/001-задача/task.md"))

    def test_genuine_management_path_survives_windows_normpath(self):
        """On Windows os.path.normpath re-introduces `\\`, so the `/`-split saw one
        element and a real management path was NOT exempted → gate blocked task
        edits. Simulated with ntpath on any host; red before the post-normpath
        separator re-normalization."""
        import ntpath
        from unittest import mock
        with mock.patch("os.path.normpath", ntpath.normpath), \
             mock.patch("os.sep", "\\"):
            self.assertTrue(_is_management_path("/proj/.agent/tasks/001-x/task.md"))
            self.assertTrue(_is_management_path("/proj/.claude/settings.json"))
            # the traversal negative must still hold under the same simulation
            self.assertFalse(_is_management_path("/proj/.agent/../src/main.py"))


class GatePathTraversal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)

    def _run(self, file_path):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = "pid-trav-test"
        env.pop("BASH_ENV", None)
        return subprocess.run(
            [bash_or_skip(), str(HOOK)], cwd=self.project, env=env, text=True,
            input=json.dumps({"tool_name": "Edit",
                              "tool_input": {"file_path": file_path}}),
            capture_output=True)

    def test_agent_traversal_to_code_is_blocked(self):
        p = str(self.project / ".agent" / ".." / "src" / "main.py")
        r = self._run(p)
        self.assertEqual(r.returncode, 2,
                         f".agent/../ traversal bypassed the gate (rc={r.returncode})")

    def test_claude_traversal_to_code_is_blocked(self):
        p = str(self.project / ".claude" / ".." / "src" / "main.py")
        r = self._run(p)
        self.assertEqual(r.returncode, 2,
                         f".claude/../ traversal bypassed the gate (rc={r.returncode})")

    def test_genuine_agent_path_still_allowed(self):
        # Negative control: a real .agent/ path is still exempt.
        p = str(self.project / ".agent" / "tasks" / "001-x" / "task.md")
        r = self._run(p)
        self.assertEqual(r.returncode, 0,
                         f"genuine .agent path was blocked: {r.stderr}")

    def test_plain_code_file_still_blocked(self):
        # Negative control: a normal code file with no task still blocks.
        r = self._run(str(self.project / "src" / "main.py"))
        self.assertEqual(r.returncode, 2)

    def test_deep_traversal_from_inside_agent_is_blocked(self):
        p = str(self.project / ".agent" / "tasks" / ".." / ".." / "src" / "main.py")
        r = self._run(p)
        self.assertEqual(r.returncode, 2,
                         f".agent/tasks/../../ traversal bypassed the gate (rc={r.returncode})")

    def test_lookalike_agent_dir_is_not_exempt(self):
        r = self._run(str(self.project / ".agentx" / "file.py"))
        self.assertEqual(r.returncode, 2,
                         f".agentx/ lookalike was exempted (rc={r.returncode})")

    def test_unicode_management_path_still_allowed(self):
        # Negative control: a genuine .agent/ path stays exempt regardless of
        # the bytes inside the task-dir name.
        p = str(self.project / ".agent" / "tasks" / "001-задача" / "task.md")
        r = self._run(p)
        self.assertEqual(r.returncode, 0,
                         f"unicode .agent path was blocked: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
