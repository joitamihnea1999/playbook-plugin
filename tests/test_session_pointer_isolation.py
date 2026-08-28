#!/usr/bin/env python3
"""A background review's sandboxed subagents must not shadow the foreground
session's active-task pointer (PB-SESSION-POINTER-ISOLATION).

The live failure (task 036): while a background `tasks panel-review` was
running, the code-edit gate reported "No active task" even though
`tasks status` showed the task active — so code had to be edited via Bash,
silently bypassing the code-edit gate.

Mechanism: every sandboxed judge/subagent funnels through
`provider/sandbox.py::_child_env`, which copied the FULL environment —
including `CLAUDE_ENV_FILE`. A claude judge seat inheriting that variable let
its own Claude-Code SessionStart hook APPEND `export PLAYBOOK_SESSION_ID=...`
to the file the FOREGROUND session sources, shadowing the foreground pointer.
The foreground's session id then resolved to an empty
`sessions/<wrong-id>/current_state`.

Fix (owning boundary): `_child_env` strips `CLAUDE_ENV_FILE` (and the
CLAUDE_CODE_* session-identity siblings) so no sandboxed subprocess can ever
write into the parent's env file. The judge keeps its own identity via the
`PLAYBOOK_SESSION_ID` the adapter sets explicitly.

Run: python3 tests/test_session_pointer_isolation.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from provider import sandbox  # noqa: E402


class ChildEnvStripsEnvFile(unittest.TestCase):
    """Unit: the single env chokepoint every sandboxed child funnels through
    must not carry the foreground's CLAUDE_ENV_FILE into the child."""

    def test_strips_claude_env_file_inherited_from_os_environ(self):
        with mock.patch.dict(os.environ,
                             {"CLAUDE_ENV_FILE": "/tmp/foreground-env-file"},
                             clear=False):
            child = sandbox._child_env(None)
        self.assertNotIn(
            "CLAUDE_ENV_FILE", child,
            "a sandboxed child must not inherit the foreground's env file — "
            "its SessionStart hook would append to it and shadow the "
            "foreground session pointer")

    def test_strips_claude_env_file_from_supplied_env(self):
        supplied = {
            "CLAUDE_ENV_FILE": "/tmp/foreground-env-file",
            "PLAYBOOK_SESSION_ID": "judge",
            "PATH": "/usr/bin",
        }
        child = sandbox._child_env(supplied)
        self.assertNotIn("CLAUDE_ENV_FILE", child)
        # The judge's own isolated identity is preserved verbatim.
        self.assertEqual(child.get("PLAYBOOK_SESSION_ID"), "judge",
                         "the adapter-set isolated session id must survive")
        self.assertEqual(child.get("PLAYBOOK_SANDBOXED"), "1")
        self.assertEqual(child.get("PATH"), "/usr/bin",
                         "unrelated env must pass through untouched")

    def test_strips_claude_code_session_identity_siblings(self):
        """The CLAUDE_CODE_* vars name the PARENT session/socket; a child that
        keeps them can attach to the parent's session channel. Strip them too."""
        supplied = {
            "CLAUDE_CODE_SSE_PORT": "9999",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_PROJECT_DIR": "/parent/project",
            "PLAYBOOK_SESSION_ID": "judge",
        }
        child = sandbox._child_env(supplied)
        for leaked in ("CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT",
                       "CLAUDE_PROJECT_DIR"):
            self.assertNotIn(leaked, child,
                             f"{leaked} names the parent session and must not "
                             "reach a sandboxed child")

    def test_does_not_invent_env_when_absent(self):
        """Stripping must be a delete, never a crash, when the var is absent."""
        base = {k: v for k, v in os.environ.items() if k != "CLAUDE_ENV_FILE"}
        child = sandbox._child_env(base)
        self.assertNotIn("CLAUDE_ENV_FILE", child)


class SandboxRunReachesTheRealSpawn(unittest.TestCase):
    """Integration: the strip must reach the actual subprocess env, not just
    the helper — this is the boundary a background judge really crosses."""

    def test_run_child_env_has_no_env_file_and_keeps_session_id(self):
        proj = Path(tempfile.mkdtemp()).resolve()
        captured = {}

        def _fake_run(wrapped, **kwargs):
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(wrapped, 0, stdout="", stderr="")

        judge_env = {
            "CLAUDE_ENV_FILE": "/tmp/foreground-env-file",
            "PLAYBOOK_SESSION_ID": "judge",
            "PATH": os.environ.get("PATH", "/usr/bin"),
        }
        with mock.patch.object(sandbox.subprocess, "run", _fake_run):
            sandbox.run("claude", ["--version"], project_root=proj,
                        env=judge_env, capture_output=True)
        env = captured["env"]
        self.assertIsNotNone(env, "sandbox.run must pass an env to subprocess")
        self.assertNotIn("CLAUDE_ENV_FILE", env,
                         "the env that actually reaches the judge spawn still "
                         "carried the foreground env file")
        self.assertEqual(env.get("PLAYBOOK_SESSION_ID"), "judge")


class PollutionVectorIsReal(unittest.TestCase):
    """Negative control: prove the test is not vacuous. A child that DOES
    inherit CLAUDE_ENV_FILE writes into the parent's file — exactly the
    shadowing the strip prevents."""

    def test_inheriting_child_pollutes_the_shared_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / "claude_env_file"
            env_file.write_text("export PLAYBOOK_SESSION_ID=pid-foreground\n",
                                encoding="utf-8")
            # A child that INHERITS the file (no strip) appends its own line,
            # just as a nested claude SessionStart hook does.
            child_env = dict(os.environ)
            child_env["CLAUDE_ENV_FILE"] = str(env_file)
            subprocess.run(
                ["bash", "-c",
                 'printf "export PLAYBOOK_SESSION_ID=%s\\n" "judge" >> "$CLAUDE_ENV_FILE"'],
                env=child_env, check=True,
            )
            contents = env_file.read_text(encoding="utf-8")
            self.assertIn("pid-foreground", contents)
            self.assertIn("judge", contents,
                          "control: an inheriting child MUST be able to pollute "
                          "the shared file — otherwise the strip proves nothing")
            # And bash's last-export-wins means the foreground would now resolve
            # the child's id, not its own.
            self.assertLess(contents.index("pid-foreground"),
                            contents.rindex("judge"),
                            "the child's export lands AFTER the foreground's — "
                            "last-wins shadows the foreground pointer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
