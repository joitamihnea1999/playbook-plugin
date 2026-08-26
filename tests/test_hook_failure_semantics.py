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
# Delivery race (external panel 2026-08-24) — monitor-nudge.sh claimed the nudge
# to a FIXED shared `.delivering` path, so concurrent hook firings collided on it
# and could drop or double-deliver. A per-invocation unique claim fixes it.
# --------------------------------------------------------------------------- #
class MonitorNudgeDeliveryRace(unittest.TestCase):
    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.cleanup)
        self.nudge = self.p.proj / ".agent" / "monitor" / "nudge.md"
        self.nudge.parent.mkdir(parents=True, exist_ok=True)

    def _run(self):
        env = dict(os.environ, PLAYBOOK_SESSION_ID=SESSION)
        env.pop("PLAYBOOK_ROLE", None)
        return subprocess.run([bash_or_skip(), str(MONITOR_NUDGE)],
                              input=json.dumps({"hook_event_name": "PostToolUse"}).encode(),
                              cwd=str(self.p.proj), env=env, capture_output=True, timeout=60)

    def test_concurrent_inflight_claim_not_clobbered(self):
        # A concurrent firing (H1) has already claimed a nudge — its in-flight
        # `.delivering` file exists. A new firing (H2) arriving with a fresh nudge
        # must NOT destroy H1's claim. The fixed SHARED `.delivering` name let
        # H2's `mv nudge.md .delivering` overwrite H1's claim, dropping (or
        # double-delivering) a nudge. Deterministic: H1's claim is materialized,
        # then one real H2 fires against it.
        inflight = Path(str(self.nudge) + ".delivering")
        inflight.write_text("INFLIGHT NUDGE (H1 mid-delivery)\n", encoding="utf-8")
        self.nudge.write_text("FRESH NUDGE for H2\n", encoding="utf-8")
        self._run()   # H2
        self.assertTrue(inflight.is_file(),
                        "a concurrent firing's in-flight nudge claim was clobbered")
        self.assertIn("INFLIGHT", inflight.read_text(encoding="utf-8"))

    def test_emit_failure_restore_survives_with_no_orphan_claim(self):
        # Judge (A3 impl review): the emit-failure restore was made atomic
        # (`ln` no-clobber, so an OLDER claim can never overwrite a NEWER nudge B
        # the monitor wrote after the claim — the no-clobber property is by
        # construction). This regression-guards the interaction that broke it: the
        # unique claim name + `ln` under `set -e` must still (a) deliver
        # at-least-once and (b) leave NO orphan `nudge.md.delivering*` claim — the
        # exact failure when `ln` aborted before cleanup.
        from tests._nopython import make_nopython_path
        self.nudge.write_text("re-read the intent\n", encoding="utf-8")
        path = make_nopython_path(self.p.tmp / "nopybin_restore")
        env = dict(os.environ, PLAYBOOK_SESSION_ID=SESSION, PATH=path)
        env.pop("PLAYBOOK_ROLE", None)
        subprocess.run([bash_or_skip(), str(MONITOR_NUDGE)],
                       input=json.dumps({"hook_event_name": "PostToolUse"}).encode(),
                       cwd=str(self.p.proj), env=env, capture_output=True, timeout=60)
        self.assertTrue(self.nudge.is_file(), "nudge lost on emit-failure restore")
        self.assertIn("re-read the intent", self.nudge.read_text(encoding="utf-8"))
        leftovers = sorted(p.name for p in self.nudge.parent.glob("nudge.md.delivering*"))
        self.assertEqual(leftovers, [], f"orphan claim left after restore: {leftovers}")

    def test_single_nudge_delivered_and_cleared(self):
        # Control: a lone nudge delivers ([MONITOR] in stdout) and fully clears —
        # no nudge.md and no leftover `.delivering*` claim of any name.
        self.nudge.write_text("re-read the intent\n", encoding="utf-8")
        r = self._run()
        self.assertIn("[MONITOR]", r.stdout.decode("utf-8", "replace"),
                      "a lone nudge was not delivered")
        self.assertFalse(self.nudge.exists(), "delivered nudge.md was not cleared")
        leftovers = sorted(str(p.name) for p in self.nudge.parent.glob("nudge.md.delivering*"))
        self.assertEqual(leftovers, [], f"leftover claim file(s): {leftovers}")


# --------------------------------------------------------------------------- #
# Orphan recovery (R4) — a crash between the per-invocation rename-claim and
# either the success `rm` or the failure `ln`-restore strands a
# `nudge.md.delivering.<pid>.<rand>` file. Later invocations only look at
# nudge.md, so that claimed nudge is silently lost (breaks at-least-once). A
# later invocation must re-land a stranded claim whose OWNER PROCESS IS DEAD,
# and must NOT steal one whose owner is still alive (a concurrent in-flight
# delivery).
# --------------------------------------------------------------------------- #
class MonitorNudgeOrphanRecovery(unittest.TestCase):
    # A pid that can never be live: above pid_max on both Linux (default
    # 2**22) and macOS (default 99999), so `kill -0` is a deterministic ESRCH.
    DEAD_PID = "2147483647"

    def setUp(self):
        self.p = _Project()
        self.addCleanup(self.p.cleanup)
        self.mon = self.p.proj / ".agent" / "monitor"
        self.mon.mkdir(parents=True, exist_ok=True)
        self.nudge = self.mon / "nudge.md"

    def _run(self):
        env = dict(os.environ, PLAYBOOK_SESSION_ID=SESSION)
        env.pop("PLAYBOOK_ROLE", None)
        return subprocess.run([bash_or_skip(), str(MONITOR_NUDGE)],
                              input=json.dumps({"hook_event_name": "PostToolUse"}).encode(),
                              cwd=str(self.p.proj), env=env, capture_output=True, timeout=60)

    def test_dead_owner_orphan_is_recovered_and_delivered(self):
        # RED before the fix: no nudge.md exists, only a stranded claim whose
        # owner pid is dead. Current code ignores `.delivering.*` and exits at
        # the `[ -f nudge.md ]` check, so the nudge is lost. After the fix the
        # invocation re-lands the orphan and delivers it.
        orphan = self.mon / f"nudge.md.delivering.{self.DEAD_PID}.abc"
        orphan.write_text("STRANDED NUDGE (dead owner)\n", encoding="utf-8")
        r = self._run()
        self.assertIn("[MONITOR]", r.stdout.decode("utf-8", "replace"),
                      "a stranded orphan of a DEAD owner was not recovered/delivered")
        self.assertIn("STRANDED NUDGE", r.stdout.decode("utf-8", "replace"))
        leftovers = sorted(x.name for x in self.mon.glob("nudge.md.delivering*"))
        self.assertEqual(leftovers, [], f"orphan claim not cleared after recovery: {leftovers}")
        self.assertFalse(self.nudge.exists(), "recovered nudge.md was not cleared after delivery")

    def test_live_owner_claim_is_not_stolen(self):
        # A claim whose owner process is STILL ALIVE is a concurrent in-flight
        # delivery, not an orphan. Recovery must leave it untouched. The owner
        # pid must be one the hook's `kill -0 "$opid"` can actually see: the
        # hook runs in (MSYS) bash, so use a REAL live bash child's `$$`, not
        # this Python process's os.getpid() — on Git-Bash a native Windows PID
        # is unknown to MSYS `kill`, which would read the owner as dead and
        # (correctly, per contract) recover the claim, failing this test.
        # Production always names claims with the hook's own bash `$$`, so this
        # mirrors the real liveness the feature checks.
        sh = bash_or_skip()
        owner = subprocess.Popen(
            [sh, "-c", "echo $$; exec sleep 30"], stdout=subprocess.PIPE)
        assert owner.stdout is not None  # PIPE is set, but keep the type checker happy
        try:
            live_pid = owner.stdout.readline().decode("utf-8", "replace").strip()
            self.assertTrue(live_pid.isdigit(), f"could not read live child pid: {live_pid!r}")
            live = self.mon / f"nudge.md.delivering.{live_pid}.xyz"
            live.write_text("IN-FLIGHT (live owner)\n", encoding="utf-8")
            self._run()
        finally:
            owner.kill()
            owner.wait(timeout=10)
            if owner.stdout:
                owner.stdout.close()
        self.assertTrue(live.is_file(),
                        "a live owner's in-flight claim was stolen by orphan recovery")
        self.assertIn("IN-FLIGHT", live.read_text(encoding="utf-8"))
        self.assertFalse(self.nudge.exists(),
                         "a live claim was re-landed as nudge.md (should be left in place)")


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
