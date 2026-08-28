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

Fix (owning boundary): `_child_env` strips the parent session-identity vars
(`CLAUDE_ENV_FILE` + the `CLAUDE_CODE_*`/`CLAUDE_PROJECT_DIR` siblings) so no
sandboxed subprocess can ever write into the parent's env file or attach to its
session. The subagent entrypoints additionally pin an isolated
`PLAYBOOK_SESSION_ID`, so a subagent never resolves the FOREGROUND session dir.

Run: python3 -m unittest tests.test_session_pointer_isolation
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
sys.path.insert(0, str(_HERE.parent))                 # repo root: `tests` package
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from provider import sandbox  # noqa: E402
from provider import subagent  # noqa: E402
from tests._bashcheck import bash_or_skip  # noqa: E402


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

    def test_strips_every_parent_session_identity_var(self):
        """The CLAUDE_CODE_* / CLAUDE_PID vars name the PARENT session, socket,
        and IPC token; a child that keeps them can resume the parent transcript
        or attach to its messaging channel. Strip the whole set (codex-sol F1)."""
        supplied = {
            "CLAUDE_ENV_FILE": "/tmp/env",
            "CLAUDE_CODE_SSE_PORT": "9999",
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_PROJECT_DIR": "/parent/project",
            "CLAUDE_CODE_SESSION_ID": "parent-session-uuid",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "CLAUDE_CODE_MESSAGING_SOCKET": "/run/parent.sock",
            "CLAUDE_CODE_MESSAGING_TOKEN": "secret-token",
            "CLAUDE_PID": "1112687",
            "PLAYBOOK_SESSION_ID": "judge",
        }
        child = sandbox._child_env(supplied)
        for leaked in ("CLAUDE_ENV_FILE", "CLAUDE_CODE_SSE_PORT",
                       "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PROJECT_DIR",
                       "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                       "CLAUDE_CODE_MESSAGING_SOCKET",
                       "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID"):
            self.assertNotIn(leaked, child,
                             f"{leaked} names the parent session and must not "
                             "reach a sandboxed child")
        # The pin the child DOES need survives.
        self.assertEqual(child.get("PLAYBOOK_SESSION_ID"), "judge")

    def test_does_not_invent_env_when_absent(self):
        """Stripping must be a delete, never a crash, when the var is absent."""
        base = {k: v for k, v in os.environ.items() if k != "CLAUDE_ENV_FILE"}
        child = sandbox._child_env(base)
        self.assertNotIn("CLAUDE_ENV_FILE", child)


class SandboxRunReachesTheRealSpawn(unittest.TestCase):
    """Integration: the strip must reach the actual subprocess env on BOTH
    run() branches — the timeout-less branch AND the timeout branch panels
    actually take (background reviews always set a timeout)."""

    def test_no_timeout_branch_child_env_is_clean(self):
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
        self.assertIsNotNone(env)
        self.assertNotIn("CLAUDE_ENV_FILE", env)
        self.assertEqual(env.get("PLAYBOOK_SESSION_ID"), "judge")

    def test_timeout_branch_child_env_is_clean(self):
        """A timeout (which every background review sets) routes run() through
        _run_with_timeout → subprocess.Popen; the child_env that branch receives
        must be the same stripped env, not the raw one (opus/sonnet F2)."""
        proj = Path(tempfile.mkdtemp()).resolve()
        captured = {}

        def _fake_rwt(wrapped, project, child_env, capture_output, check, kwargs):
            captured["env"] = child_env
            return subprocess.CompletedProcess(wrapped, 0, stdout="", stderr="")

        with mock.patch.object(sandbox, "_run_with_timeout", _fake_rwt):
            sandbox.run("claude", ["--version"], project_root=proj,
                        env={"CLAUDE_ENV_FILE": "/tmp/env",
                             "PLAYBOOK_SESSION_ID": "judge",
                             "PATH": os.environ.get("PATH", "/usr/bin")},
                        capture_output=True, timeout=5)
        env = captured["env"]
        self.assertIsNotNone(env, "the timeout branch must receive a child_env")
        self.assertNotIn("CLAUDE_ENV_FILE", env,
                         "the env reaching the REAL background-judge spawn "
                         "(timeout branch) still carried the foreground env file")
        self.assertEqual(env.get("PLAYBOOK_SESSION_ID"), "judge")


class SubagentDoesNotShareForegroundSession(unittest.TestCase):
    """run_subagent/stream_subagent must pin an ISOLATED PLAYBOOK_SESSION_ID —
    otherwise `_child_env(None)` preserves the foreground id and the subagent
    resolves (and on SessionEnd could delete) the foreground session dir
    (codex-sol F2)."""

    def test_run_subagent_env_does_not_carry_foreground_session(self):
        proj = Path(tempfile.mkdtemp()).resolve()
        captured = {}

        def _fake_run(agent, argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        spec = subagent.SubagentSpec(agent="claude", prompt="hi", timeout_secs=300)
        with mock.patch.dict(os.environ,
                             {"PLAYBOOK_SESSION_ID": "pid-foreground-1112687"},
                             clear=False), \
             mock.patch.object(subagent._sandbox, "run", _fake_run):
            subagent.run_subagent(spec, project_root=proj)
        env = captured["env"]
        self.assertIsNotNone(env, "run_subagent must pass an explicit env")
        self.assertNotEqual(
            env.get("PLAYBOOK_SESSION_ID"), "pid-foreground-1112687",
            "a subagent must not inherit the foreground session id — it could "
            "delete the foreground session dir on SessionEnd")
        self.assertTrue(
            str(env.get("PLAYBOOK_SESSION_ID", "")).startswith("subagent-"),
            f"expected an isolated subagent id, got {env.get('PLAYBOOK_SESSION_ID')!r}")

    def test_two_subagents_get_distinct_ids(self):
        """Unique-per-invocation ids: two concurrent subagents must not share a
        session dir (so one's SessionEnd can't reclaim the other's)."""
        proj = Path(tempfile.mkdtemp()).resolve()
        seen = []

        def _fake_run(agent, argv, **kwargs):
            seen.append(kwargs.get("env", {}).get("PLAYBOOK_SESSION_ID"))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        spec = subagent.SubagentSpec(agent="claude", prompt="hi", timeout_secs=300)
        with mock.patch.object(subagent._sandbox, "run", _fake_run):
            subagent.run_subagent(spec, project_root=proj)
            subagent.run_subagent(spec, project_root=proj)
        self.assertEqual(len(set(seen)), 2, f"ids collided: {seen}")


class PollutionVectorIsReal(unittest.TestCase):
    """Negative control: prove the test is not vacuous. A child that DOES
    inherit CLAUDE_ENV_FILE writes into the parent's file — exactly the
    shadowing the strip prevents."""

    def test_inheriting_child_pollutes_the_shared_env_file(self):
        bash = bash_or_skip()   # skip (not error) where bash is unusable (win lane)
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / "claude_env_file"
            env_file.write_text("export PLAYBOOK_SESSION_ID=pid-foreground\n",
                                encoding="utf-8")
            # A child that INHERITS the file (no strip) appends its own line,
            # just as a nested claude SessionStart hook does.
            child_env = dict(os.environ)
            child_env["CLAUDE_ENV_FILE"] = str(env_file)
            subprocess.run(
                [bash, "-c",
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
