#!/usr/bin/env python3
"""Enforcement failure semantics: "the guard could not run" => fail OPEN, loudly.

Audit (origin/main 24d37ee) found enforcing hooks that fail CLOSED — wedging the
session — when a helper file is missing, because `python3 <missing-file>` exits 2
and PreToolUse reads 2 as BLOCK. The owner decision: a guard that cannot run
allows the tool and says so loudly on stderr (the OS sandbox, not a pattern hook,
is the security boundary; bricking every shell call on a partial install is
unacceptable cost for no security gain). This module pins that polarity for every
defect the audit measured, and proves genuine blocks still block.

All six are Linux-reproducible: delete the helper in a /tmp copy of scripts/,
strip python3 from PATH, or feed malformed JSON.

Run: python3 -m unittest tests.test_hook_failure_semantics
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._bashcheck import bash_or_skip
from tests._nopython import make_nopython_path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins" / "playbook"
SCRIPTS = PLUGIN / "scripts"
MONITOR_NUDGE = PLUGIN / "hooks" / "monitor-nudge.sh"

SESSION = "pid-failsem-test"

BARE_GATES = ["- [ ] G1: run the suite", "- [ ] G2: update the mind map"]


def _checked(line: str, note: str = "") -> str:
    return line.replace("- [ ]", "- [x]", 1) + note


class _Project:
    """A minimal playbook project with one active task (001)."""

    def __init__(self, gates=BARE_GATES):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "proj"
        task_dir = self.proj / ".agent" / "tasks" / "001-thing"
        task_dir.mkdir(parents=True)
        self.task_file = task_dir / "task.md"
        self.task_file.write_text(
            "# 001 - Thing\n\n## Status\npending\n\n## Work Plan\n"
            + "\n".join(gates) + "\n", encoding="utf-8")
        self.session_dir = self.proj / ".agent" / "sessions" / SESSION
        self.session_dir.mkdir(parents=True)
        (self.session_dir / "current_state").write_text("001\n", encoding="utf-8")
        (self.session_dir / "counters").write_text("tools=9\nwrites=3\n", encoding="utf-8")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _copy_scripts(dest: Path) -> Path:
    """Copy the whole scripts/ tree so a single helper can be removed without
    touching the repo. Returns the copied scripts dir."""
    shutil.copytree(SCRIPTS, dest)
    return dest


def _run(script: Path, payload: dict, cwd: Path, env_extra=None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PLAYBOOK_SESSION_ID=SESSION)
    env.pop("PLAYBOOK_ROLE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([bash_or_skip(), str(script)],
                          input=json.dumps(payload).encode(),
                          cwd=str(cwd), env=env, capture_output=True, timeout=60)


# --------------------------------------------------------------------------- #
# Defect 1 — command-guard-hook: missing helper must fail OPEN, loudly.
# --------------------------------------------------------------------------- #
class CommandGuardMissingHelper(unittest.TestCase):
    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.cleanup)
        self.scr = _copy_scripts(self.p.tmp / "scripts")

    def _payload(self, command):
        return {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": command}}

    def test_missing_helper_fails_open_not_blocks_all_shell(self):
        (self.scr / "command_guard.py").unlink()
        r = _run(self.scr / "command-guard-hook", self._payload("ls -la"), self.p.proj)
        self.assertEqual(r.returncode, 0,
                         f"missing command_guard.py blocked a safe shell call (wedge): {r.stderr.decode('utf-8', 'replace')}")

    def test_missing_helper_warns_loudly_naming_path(self):
        (self.scr / "command_guard.py").unlink()
        r = _run(self.scr / "command-guard-hook", self._payload("ls -la"), self.p.proj)
        err = r.stderr.decode('utf-8', 'replace')
        self.assertIn("command_guard.py", err,
                      f"fail-open was silent; stderr must name the missing helper: {err!r}")

    def test_unreadable_helper_fails_open(self):
        # POSIX-only: chmod 000 does not remove read on native Windows, and
        # os.geteuid is absent there. The missing-file case above covers the
        # cross-platform "helper cannot run" polarity.
        if os.name == "nt" or not hasattr(os, "geteuid"):
            self.skipTest("chmod-based unreadability is POSIX-only")
        if os.geteuid() == 0:
            self.skipTest("root ignores file permission bits")
        helper = self.scr / "command_guard.py"
        helper.chmod(0o000)
        self.addCleanup(lambda: helper.chmod(0o644))
        r = _run(self.scr / "command-guard-hook", self._payload("ls -la"), self.p.proj)
        self.assertEqual(r.returncode, 0,
                         f"unreadable command_guard.py wedged shell: {r.stderr.decode('utf-8', 'replace')}")

    def test_genuine_dangerous_command_still_blocks_with_helper_present(self):
        r = _run(self.scr / "command-guard-hook",
                 self._payload("git push --force origin main"), self.p.proj)
        self.assertEqual(r.returncode, 2,
                         f"real dangerous command must still block: {r.stdout.decode('utf-8', 'replace')} {r.stderr.decode('utf-8', 'replace')}")


# --------------------------------------------------------------------------- #
# Defect 2 — task-gate-hook: missing gate-batch-check.py must fail OPEN, loudly,
# WITHOUT suppressing a genuine batch block.
# --------------------------------------------------------------------------- #
class BatchGuardMissingHelper(unittest.TestCase):
    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.cleanup)
        self.scr = _copy_scripts(self.p.tmp / "scripts")

    def _batch_close_payload(self):
        old = BARE_GATES
        new = [_checked(g) for g in BARE_GATES]   # 2 bare closes → a real block
        return {"hook_event_name": "PreToolUse", "tool_name": "Edit",
                "tool_input": {"file_path": str(self.p.task_file),
                               "old_string": "\n".join(old),
                               "new_string": "\n".join(new)}}

    def test_missing_helper_fails_open_not_empty_block(self):
        (self.scr / "gate-batch-check.py").unlink()
        r = _run(self.scr / "task-gate-hook", self._batch_close_payload(), self.p.proj)
        self.assertEqual(r.returncode, 0,
                         f"missing gate-batch-check.py blocked a task.md edit: {r.stderr.decode('utf-8', 'replace')!r}")

    def test_missing_helper_warns_loudly_naming_path(self):
        (self.scr / "gate-batch-check.py").unlink()
        r = _run(self.scr / "task-gate-hook", self._batch_close_payload(), self.p.proj)
        err = r.stderr.decode('utf-8', 'replace')
        self.assertIn("gate-batch-check.py", err,
                      f"fail-open was silent; stderr must name the missing helper: {err!r}")

    def test_genuine_bare_batch_still_blocks_with_helper_present(self):
        r = _run(self.scr / "task-gate-hook", self._batch_close_payload(), self.p.proj)
        self.assertEqual(r.returncode, 2,
                         f"a real bare batch close must still block: {r.stdout.decode('utf-8', 'replace')} {r.stderr.decode('utf-8', 'replace')}")
        self.assertIn("BLOCKED", r.stderr.decode('utf-8', 'replace'))


# --------------------------------------------------------------------------- #
# Defect 3 — chat-log-hook: malformed JSON must not abort the hook (set -e + jq).
# --------------------------------------------------------------------------- #
class ChatLogMalformedJson(unittest.TestCase):
    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.cleanup)

    def _run_raw(self, raw: bytes):
        env = dict(os.environ, PLAYBOOK_SESSION_ID=SESSION)
        env.pop("PLAYBOOK_ROLE", None)
        return subprocess.run([bash_or_skip(), str(SCRIPTS / "chat-log-hook")],
                              input=raw, cwd=str(self.p.proj),
                              env=env, capture_output=True, timeout=60)

    def test_malformed_json_does_not_abort(self):
        if shutil.which("jq") is None:
            self.skipTest("jq not installed; the abort is jq-specific")
        r = self._run_raw(b"garbage{{ not json")
        self.assertEqual(r.returncode, 0,
                         f"chat-log aborted on malformed JSON (jq exit propagated): rc={r.returncode}")

    def test_malformed_json_reaches_raw_fallback_and_logs(self):
        if shutil.which("jq") is None:
            self.skipTest("jq not installed")
        self._run_raw(b"garbage{{ not json")
        log = self.p.proj / ".agent" / "chat_log.md"
        self.assertTrue(log.is_file() and log.read_text(encoding="utf-8").strip(),
                        "the raw-input fallback is dead code: nothing was logged")


# --------------------------------------------------------------------------- #
# Defect 4 — state-echo-hook: python3 absent must not emit malformed JSON.
# --------------------------------------------------------------------------- #
class StateEchoNoPython(unittest.TestCase):
    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.cleanup)

    def test_output_is_valid_json_without_python3(self):
        path = make_nopython_path(self.p.tmp / "nopybin")
        r = _run(SCRIPTS / "state-echo-hook",
                 {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                  "tool_input": {"command": "ls"}},
                 self.p.proj, env_extra={"PATH": path})
        self.assertEqual(r.returncode, 0, r.stderr.decode('utf-8', 'replace'))
        out = r.stdout.decode('utf-8', 'replace').strip()
        self.assertTrue(out, "state-echo emitted nothing at all")
        try:
            json.loads(out)
        except json.JSONDecodeError as e:
            self.fail(f"state-echo emitted invalid JSON without python3: {e}\n{out!r}")


# --------------------------------------------------------------------------- #
# Defect 5 — monitor-nudge.sh: a nudge it cannot deliver must not be lost.
# --------------------------------------------------------------------------- #
class MonitorNudgePreserved(unittest.TestCase):
    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.cleanup)
        # legacy layout: root .agent/ is the lane (no current_user marker)
        self.nudge = self.p.proj / ".agent" / "monitor" / "nudge.md"
        self.nudge.parent.mkdir(parents=True, exist_ok=True)
        self.nudge.write_text("please re-read the intent\n", encoding="utf-8")

    def test_nudge_not_consumed_when_python3_absent(self):
        path = make_nopython_path(self.p.tmp / "nopybin2")
        env = dict(os.environ, PLAYBOOK_SESSION_ID=SESSION, PATH=path)
        env.pop("PLAYBOOK_ROLE", None)
        subprocess.run([bash_or_skip(), str(MONITOR_NUDGE)],
                       input=json.dumps({"hook_event_name": "PostToolUse"}).encode(),
                       cwd=str(self.p.proj), env=env, capture_output=True, timeout=60)
        delivering = Path(str(self.nudge) + ".delivering")
        self.assertTrue(self.nudge.is_file() or delivering.is_file(),
                        "nudge was consumed but not delivered (lost)")
        surviving = self.nudge if self.nudge.is_file() else delivering
        self.assertIn("re-read the intent", surviving.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Ledger evidence — PB-COMMAND-FAILURE-POLICY: a malformed PROJECT
# dangerous_commands regex is skipped (except re.error) and does NOT disable the
# built-in patterns. The audit measured this; bind it as executable evidence.
# --------------------------------------------------------------------------- #
class MalformedProjectRegexIsSafe(unittest.TestCase):
    GUARD = SCRIPTS / "command_guard.py"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.proj = self.tmp / "proj"
        (self.proj / ".agent" / "tasks").mkdir(parents=True)
        (self.proj / ".agent" / "config.json").write_text(
            json.dumps({"dangerous_commands": ["(unclosed"]}), encoding="utf-8")

    def _run(self, command):
        env = dict(os.environ)
        env.pop("PLAYBOOK_ALLOW_DANGEROUS", None)
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        return subprocess.run(["python3", str(self.GUARD)],
                              input=json.dumps(payload), text=True,
                              cwd=str(self.proj), env=env, capture_output=True, timeout=60)

    def test_malformed_project_regex_does_not_crash_or_block_safe(self):
        r = self._run("ls -la")
        self.assertEqual(r.returncode, 0,
                         f"a malformed project regex must be skipped, not crash/block: {r.stderr}")

    def test_builtin_patterns_survive_a_malformed_project_regex(self):
        r = self._run("git push --force origin main")
        self.assertEqual(r.returncode, 2,
                         "a bad project regex must NOT disable the built-in destructive-command guard")


# --------------------------------------------------------------------------- #
# Defect 6 — stop-hook: stale-pointer intent is documented (no behaviour change).
# --------------------------------------------------------------------------- #
class StopHookStalePointerIntent(unittest.TestCase):
    def test_source_states_the_stale_pointer_intent(self):
        src = (SCRIPTS / "stop-hook").read_text(encoding="utf-8")
        # The comment must explain WHY an unresolvable pointer allows the stop
        # (contrast task-gate-hook, which blocks an unresolvable pointer).
        self.assertRegex(
            src, r"task file not found|unresolvable|stale pointer|no gates to enforce",
            "stop-hook's stale-pointer allow path has no stated intent comment")

    def test_stale_pointer_still_allows_stop(self):
        # Behaviour pin: an unresolvable pointer allows the stop (there is no
        # task dir whose gates could be enforced). MUST NOT change.
        p = _Project()
        self.addCleanup(p.cleanup)
        (p.session_dir / "current_state").write_text("777\n", encoding="utf-8")  # no such task
        r = _run(SCRIPTS / "stop-hook", {"hook_event_name": "Stop", "stop_hook_active": False}, p.proj)
        self.assertEqual(r.returncode, 0,
                         f"stale-pointer stop must remain allowed: {r.stderr.decode('utf-8', 'replace')}")


if __name__ == "__main__":
    unittest.main()
