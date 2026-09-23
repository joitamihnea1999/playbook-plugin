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

    def test_cr_suffix_does_not_forge_or_break_management(self):
        # The bare-CR trick (fix/cr-path-parity) rides the TRAILING path
        # component, so it can neither forge a `.agent`/`.claude` component nor
        # break a genuine one that sits earlier in the path — the management
        # exemption stays consistent under it (no divergence, no fix needed).
        # A trailing CR on a code file is still NOT management (would fall to the
        # code-file test and gate):
        self.assertFalse(_is_management_path("/proj/src/main.py\r"))
        self.assertFalse(_is_management_path("/proj/src/main.py\r\n"))
        # A CR-mangled `.agent`/`.claude` token is not the component (fail-safe):
        self.assertFalse(_is_management_path("/proj/.agent\r/main.py"))
        # A genuine management path with a trailing CR on the LAST component is
        # still management — the exempted component is unmangled:
        self.assertTrue(_is_management_path("/proj/.agent/tasks/001-x/task.md\r"))
        self.assertTrue(_is_management_path("/proj/.claude/settings.json\r\n"))

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

    def test_bare_cr_code_path_still_blocked(self):
        # End-to-end (fix/cr-path-parity): a code file with a trailing CR and NO
        # code-dir component reaches is_code_file_path as the classifier's only
        # gate. Before the fix bash kept the \r inside the extension, missed the
        # code-ext match, found no code dir, and FAILED OPEN (rc 0) — while the
        # Codex/Python twin gated the same path. Must block with no active task.
        r = self._run(str(self.project / "main.py") + "\r")
        self.assertEqual(r.returncode, 2,
                         f"bare-CR code path bypassed the gate (rc={r.returncode})")

    def test_crlf_code_path_still_blocked(self):
        r = self._run(str(self.project / "main.py") + "\r\n")
        self.assertEqual(r.returncode, 2,
                         f"CRLF code path bypassed the gate (rc={r.returncode})")

    def test_cr_doc_path_still_allowed(self):
        # Negative control: a trailing CR on a NON-code file must not start
        # blocking edits it never blocked — README.md\r stays exempt.
        r = self._run(str(self.project / "README.md") + "\r")
        self.assertEqual(r.returncode, 0,
                         f"CR doc path was wrongly blocked: {r.stderr}")


class ManualTaskDirGuard(unittest.TestCase):
    """PLAN S1d (task 073 flag C15, measured live by task 079 and again by task
    080): "don't create task directories manually" matched the WHOLE command
    text, so a temp-dir fixture outside the project (`<tmp>/.agent/tasks/001-x`)
    was refused. Only a target that is — or may be — under the project root
    is a task dir. The literal `mkdir` is assembled at runtime so this file's
    own text never trips the guard it tests."""

    MK = "mk" + "dir"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Forward-slash spellings: POSIX shell quoting eats backslashes, so a
        # Windows `C:\\…` literal is not what anyone types into Git Bash.
        self.base = Path(self._tmp.name)
        (self.base / "proj" / ".agent" / "tasks").mkdir(parents=True)
        self.project = (self.base / "proj").as_posix()
        self.outside = (self.base / "fixture").as_posix()   # a sibling temp dir, NOT under proj

    def _run(self, command):
        env = dict(os.environ, PLAYBOOK_SESSION_ID="pid-mkdir-test")
        env.pop("BASH_ENV", None)
        return subprocess.run(
            [bash_or_skip(), str(HOOK)], cwd=self.base / "proj", env=env, text=True,
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True)

    def test_temp_dir_agent_tasks_is_not_a_task_dir(self):
        for cmd in (f"{self.MK} -p {self.outside}/.agent/tasks/001-x",
                    f"{self.MK} -p {self.outside}/.agent/tasks",
                    f"{self.MK} -p {self.outside}/.agent/alice/tasks/001-x",
                    f"{self.MK} -p '{self.outside}/.agent/tasks/001-x'",
                    f"{self.MK} -m 755 -p {self.outside}/.agent/tasks/001-x/sub",
                    f"/bin/{self.MK} -p {self.outside}/.agent/tasks/001-x"):
            r = self._run(cmd)
            self.assertEqual(r.returncode, 0, f"temp fixture refused: {cmd!r}: {r.stderr}")

    def test_only_a_simple_mkdir_is_ever_allowed(self):
        # Round 2 (2 seats): the helper judges the filesystem BEFORE the command
        # runs, so an earlier step of a compound command can repoint the path
        # (`ln -s <proj> <tmp>/late; mkdir -p <tmp>/late/...`). Only a single,
        # simple mkdir with no expansion anywhere is judged; the rest is refused.
        late = f"{self.base.as_posix()}/late"
        tail = f"{self.MK} -p {self.outside}/.agent/tasks/001-x"
        for cmd in (f"ln -s {self.project} {late}; {self.MK} -p {late}/.agent/tasks/999-x",
                    f"cd /tmp && {tail}",
                    f"{tail} | cat",
                    f"{tail} &",
                    f"({tail})",
                    f"echo $(ln -s {self.project} {late}) {tail}",
                    f"true\n{tail}",
                    f"X=1 {tail}"):
            r = self._run(cmd)
            self.assertEqual(r.returncode, 2, f"a non-simple command was judged: {cmd!r}")

    def test_project_agent_tasks_is_still_blocked(self):
        mk = self.MK
        for cmd in (f"{mk} -p {self.project}/.agent/tasks/001-x",
                    f"{mk} -p .agent/tasks/001-x",
                    f"{mk} -p ./.agent/tasks/001-x",
                    f"{mk} -p {self.project}/.agent/alice/tasks/001-x",
                    f"{mk} -p ~/.agent/tasks/001-x",
                    f"{mk} -p $HOME/.agent/tasks/001-x",
                    f"{mk} -p \"$PWD\"/.agent/tasks/001-x",
                    f"{mk} -p {self.outside}/../proj/.agent/tasks/001-x",
                    f"{mk} -p {self.outside}/.agent/tasks/001-x && {mk} -p .agent/tasks/002-y",
                    f"{mk} -p {self.outside}/.agent/tasks/001-x {self.project}/.agent/tasks/002-y",
                    # Round 2 (grok): spellings the old trigger never matched.
                    f"{mk} -p {self.project}/.agent/foo/../tasks/001-x",
                    f"{mk} -p {self.project}//.agent//tasks//001-x"):
            r = self._run(cmd)
            self.assertEqual(r.returncode, 2, f"in-project task dir allowed: {cmd!r}")
            self.assertIn("task directories manually", r.stderr)

    def test_symlink_into_project_is_still_blocked(self):
        # A literal absolute path OUTSIDE the project that resolves INTO it is
        # still a task dir — inside-ness is judged through the filesystem too.
        link = self.base / "link-to-proj"
        try:
            os.symlink(self.base / "proj", link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable (unprivileged Windows)")
        r = self._run(f"{self.MK} -p {link.as_posix()}/.agent/tasks/001-x")
        self.assertEqual(r.returncode, 2, f"symlink into the project allowed: {r.stderr}")
        # Control: the helper still allows a real outside path in the same run shape.
        r = self._run(f"{self.MK} -p {self.outside}/.agent/tasks/001-x")
        self.assertEqual(r.returncode, 0, r.stderr)

    def _link_or_skip(self, target, link):
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable (unprivileged Windows)")

    def test_symlink_into_project_subdirectory_is_still_blocked(self):
        # Round 1 (4 seats): samefile against the ROOT missed a link into a subdir.
        (self.base / "proj" / "src").mkdir()
        link = self.base / "sublink"
        self._link_or_skip(self.base / "proj" / "src", link)
        r = self._run(f"{self.MK} -p {link.as_posix()}/.agent/tasks/001-x")
        self.assertEqual(r.returncode, 2, f"subdir symlink into the project allowed: {r.stderr}")

    def test_symlinked_alias_with_dotdot_through_missing_dir_is_blocked(self):
        # CI macOS (/var -> /private/var): the project is reached through its
        # physical path, the token through an alias plus `missing/..`.
        alias = self.base / "alias"
        self._link_or_skip(self.base, alias)
        r = self._run(f"{self.MK} -p {alias.as_posix()}/missing/../proj/.agent/tasks/001-x")
        self.assertEqual(r.returncode, 2, f"alias + missing/.. allowed: {r.stderr}")

    def test_shell_expansion_is_not_judged_literally(self):
        # Round 1: the shell expands these; a literal reading judged them outside.
        (self.base / "fixture").mkdir()
        b = self.base.as_posix()
        for cmd in (f"{self.MK} -p {b}/{{fixture,proj}}/.agent/tasks/001-x",
                    f"{self.MK} -p {b}/*/.agent/tasks/001-x",
                    f"{self.MK} -p {b}/pro?/.agent/tasks/001-x",
                    f"{self.MK} -p {b}/[p]roj/.agent/tasks/001-x"):
            r = self._run(cmd)
            self.assertEqual(r.returncode, 2, f"shell expansion judged literally: {cmd!r}")

    @unittest.skipIf(os.name == "nt", "a drive letter IS absolute on Windows")
    def test_drive_letter_is_relative_on_posix(self):
        # Round 1: `C:/x` is a RELATIVE path on POSIX — it lands under the cwd.
        r = self._run(f"{self.MK} -p C:/tmp/.agent/tasks/001-x")
        self.assertEqual(r.returncode, 2, f"POSIX-relative C:/ path allowed: {r.stderr}")

    def test_guard_runs_before_session_injection(self):
        # Round 1: `tasks status; …` hit the session-injection early allow first.
        for cmd in (f"tasks status; {self.MK} -p {self.project}/.agent/tasks/999-x",
                    f".claude/bin/tasks work 3 && {self.MK} -p .agent/tasks/999-x"):
            r = self._run(cmd)
            self.assertEqual(r.returncode, 2, f"guard bypassed via injection: {cmd!r}")
        # Control: the injection itself still works for a plain tasks call.
        r = self._run("tasks status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PLAYBOOK_SESSION_ID", r.stdout)


class ManualTaskDirHelperPortability(unittest.TestCase):
    """task-dir-target.py's Windows spelling rules, exercised on any host by
    simulating `os.name == "nt"` (the real Windows lane runs the hook tests)."""

    def _mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "task_dir_target", HOOK.parent / "task-dir-target.py")
        assert spec is not None and spec.loader is not None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_nt_dotdot_is_collapsed_before_drive_conversion(self):
        # Round 1 (grok): converting `/c/` before collapsing `..` let
        # `/tmp/../c/proj/...` escape the project comparison.
        from unittest import mock
        m = self._mod()
        with mock.patch.object(m.os, "name", "nt"):
            self.assertTrue(m._lexically_inside("/tmp/../c/proj/.agent/tasks/1", "/c/proj"))
            self.assertTrue(m._lexically_inside("C:/Proj/.agent/tasks/1", "/c/proj"))
            self.assertFalse(m._lexically_inside("D:/fixture/.agent/tasks/1", "/c/proj"))

    def test_nt_msys_rooted_token_may_be_inside(self):
        # `/tmp/...` under Git Bash is an MSYS mount Python cannot resolve.
        from unittest import mock
        m = self._mod()
        with mock.patch.object(m.os, "name", "nt"):
            self.assertTrue(m.may_be_inside("mk" "dir -p /tmp/x/.agent/tasks/1", "/c/proj"))


if __name__ == "__main__":
    unittest.main()
