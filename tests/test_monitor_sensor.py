#!/usr/bin/env python3
"""Monitor skill — mechanical seams (the skill shipped with ZERO tests).

The monitor is a second Claude agent watching the front agent's session JSONL,
keeping a judgment trace, and nudging via a hook. What the monitor DECIDES —
whether a trajectory warrants a nudge, what the wake judgment says — is pure
LLM work and is deliberately NOT tested here (a fixture asserting on model
output would pin nothing). What earns tests is everything mechanical around
that decision, where a silent defect starves or garbles the LLM's inputs and
outputs:

  * `sensor.py read_new_events` — JSONL extraction (user/assistant/tool/
    thinking/turn_end), noise + isMeta filters, malformed-line resilience,
    BYTE-offset arithmetic (multi-byte UTF-8 — a char-based offset would
    silently re-read or skip), stop_after_turn_end resume semantics;
  * offset persistence — atomic save, round-trip, incremental reads return
    only new events;
  * `wait_once` — cold start seeds at EOF (a monitor must not replay the
    whole session), turn_end flush persists the offset, stall flush frees a
    partial turn from a crashed agent, a dead front-agent pid exits instead
    of blocking forever;
  * `hooks/monitor-nudge.sh` — the delivery seam: a pending nudge.md is
    atomically consumed, emitted as additionalContext, and logged to
    chat_log; NO nudge means NO output (the trajectory-that-must-not-nudge
    negative control); the monitor's own session never consumes its own
    nudge (PLAYBOOK_ROLE=monitor); a malformed multi-user marker delivers
    nothing rather than reading the wrong lane;
  * `bootstrap.sh` guards — refuses to run without a project dir or session
    id, rejects a shell-metacharacter SESSION_ID (path-traversal / sandbox-
    injection guard), and on the happy path emits the COMMANDS briefing and
    seeds the offset file at the JSONL's current EOF.

Run: python3 -m unittest tests.test_monitor_sensor
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from tests._bashcheck import bash_or_skip
import sys
import tempfile
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
MONITOR_LIB = PLUGIN / "scripts" / "monitor-lib"
NUDGE_HOOK = PLUGIN / "hooks" / "monitor-nudge.sh"

_spec = importlib.util.spec_from_file_location("sensor", MONITOR_LIB / "sensor.py")
sensor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sensor)


def _jl(obj) -> bytes:
    # ensure_ascii=False: real session JSONL is written by Node's
    # JSON.stringify, which emits raw UTF-8, not \uXXXX escapes — and the
    # multibyte-offset test is toothless on an all-ASCII file (found by
    # mutation: a char-based offset passed against the escaped form).
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def user_line(text, ts="2026-08-13T10:00:00Z", meta=False):
    d = {"type": "user", "timestamp": ts, "message": {"content": text}}
    if meta:
        d["isMeta"] = True
    return _jl(d)


def assistant_line(blocks, stop_reason=None, ts="2026-08-13T10:00:01Z"):
    return _jl({"type": "assistant", "timestamp": ts,
                "message": {"content": blocks, "stop_reason": stop_reason}})


def tool_block(name, inp):
    return {"type": "tool_use", "name": name, "input": inp}


def text_block(text):
    return {"type": "text", "text": text}


FULL_TURN = (
    user_line("please fix the flaky test — héllo ünïcode")
    + assistant_line([text_block("Looking at it."),
                      tool_block("Read", {"file_path": "/proj/src/code.py"})])
    + assistant_line([tool_block("Edit", {"file_path": "/proj/src/code.py",
                                          "old_string": "x = 1"})])
    + assistant_line([tool_block("Bash", {"command": "python3 -m unittest -v"})])
    + assistant_line([text_block("Fixed.")], stop_reason="end_turn")
)


class ReadNewEvents(unittest.TestCase):
    def _events(self, raw: bytes, offset=0, **kw):
        p = Path(tempfile.mkdtemp()) / "s.jsonl"
        p.write_bytes(raw)
        return sensor.read_new_events(p, offset, **kw), p

    def test_full_turn_extraction(self):
        (events, off), p = self._events(FULL_TURN)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["user", "assistant_text", "tool", "tool",
                                 "tool", "assistant_text", "turn_end"])
        self.assertEqual(off, p.stat().st_size, "byte offset must land at EOF")
        tools = [e for e in events if e["type"] == "tool"]
        self.assertEqual([t["phase"] for t in tools], ["orient", "execute", "verify"])
        self.assertEqual(tools[0]["detail"], "code.py")

    def test_noise_and_meta_filtered(self):
        raw = (user_line("/model") + user_line("!ls") +
               user_line("<task-notification> done") +
               user_line("real message") + user_line("hidden", meta=True))
        (events, _), _ = self._events(raw)
        self.assertEqual([e["preview"] for e in events], ["real message"])

    def test_malformed_line_skipped_rest_parsed(self):
        raw = b'{"type": "user", GARBAGE\n' + user_line("after the garbage")
        (events, off), p = self._events(raw)
        self.assertEqual([e["preview"] for e in events], ["after the garbage"])
        self.assertEqual(off, p.stat().st_size)

    def test_multibyte_offset_resume_reads_nothing_extra(self):
        # The offset is BYTES: after reading a turn containing multi-byte
        # UTF-8, resuming from the returned offset must see zero new events —
        # a char-based offset would re-read the tail or split a line.
        p = Path(tempfile.mkdtemp()) / "s.jsonl"
        p.write_bytes(FULL_TURN)
        _, off = sensor.read_new_events(p, 0)
        events2, off2 = sensor.read_new_events(p, off)
        self.assertEqual(events2, [])
        self.assertEqual(off2, off)

    def test_stop_after_turn_end_resumes_at_next_turn(self):
        second = user_line("second request") + assistant_line(
            [text_block("on it")], stop_reason="end_turn")
        p = Path(tempfile.mkdtemp()) / "s.jsonl"
        p.write_bytes(FULL_TURN + second)
        events, off = sensor.read_new_events(p, 0, stop_after_turn_end=True)
        self.assertEqual(events[-1]["type"], "turn_end")
        self.assertNotIn("second request", json.dumps(events))
        events2, off2 = sensor.read_new_events(p, off)
        self.assertEqual([e["type"] for e in events2],
                         ["user", "assistant_text", "turn_end"])
        self.assertEqual(off2, p.stat().st_size)

    def test_thinking_block_becomes_marker(self):
        raw = assistant_line([{"type": "thinking", "thinking": ""}])
        (events, _), _ = self._events(raw)
        self.assertEqual(events[0]["type"], "assistant_thinking")
        self.assertEqual(events[0]["preview"], "(content redacted)")


class PhaseAndDetail(unittest.TestCase):
    def test_phase_table(self):
        for tool, phase in [("Read", "orient"), ("Grep", "orient"),
                            ("Edit", "execute"), ("Write", "execute"),
                            ("Bash", "verify"), ("Skill", "meta"),
                            ("SomethingNew", "other")]:
            self.assertEqual(sensor.classify_phase(tool), phase, tool)

    def test_bash_detail_truncated(self):
        d = sensor._extract_detail("Bash", {"command": "x" * 200})
        self.assertEqual(len(d), 80)


class FormatCompact(unittest.TestCase):
    def test_spans_grouped_between_user_messages(self):
        events, _ = (lambda p: sensor.read_new_events(p, 0))(
            self._write(FULL_TURN + user_line("now the docs")
                        + assistant_line([tool_block("Write", {"file_path": "/d/doc.md"})])))
        out = sensor.format_compact(events)
        self.assertIn("**USER:** please fix the flaky test", out)
        self.assertIn("### Span 1: 3 calls", out)
        self.assertIn("### Span 2: 1 calls", out)
        self.assertIn("[E] Edit", out)
        self.assertIn("[V] Bash", out)

    @staticmethod
    def _write(raw: bytes) -> Path:
        p = Path(tempfile.mkdtemp()) / "s.jsonl"
        p.write_bytes(raw)
        return p


class OffsetPersistence(unittest.TestCase):
    def test_round_trip_and_missing(self):
        d = Path(tempfile.mkdtemp())
        f = d / ".offset"
        self.assertIsNone(sensor.load_offset(f))
        sensor.save_offset(f, 12345)
        self.assertEqual(sensor.load_offset(f), 12345)
        self.assertFalse(list(d.glob("*.tmp")), "atomic save left a temp file")


class WaitOnce(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.jsonl = self.dir / "s.jsonl"
        self.offset_file = self.dir / ".offset"

    def test_cold_start_seeds_at_eof_and_ignores_history(self):
        self.jsonl.write_bytes(FULL_TURN)  # pre-existing history
        events, off = sensor.wait_once(self.jsonl, self.offset_file,
                                       interval=0.02, max_wait=0.15)
        self.assertEqual(events, [], "cold start replayed old history")
        self.assertEqual(off, self.jsonl.stat().st_size)
        self.assertEqual(sensor.load_offset(self.offset_file), off)
        # A new turn appended after the seed IS returned, and flushes on
        # turn_end with the offset persisted.
        with self.jsonl.open("ab") as f:
            f.write(user_line("new ask") + assistant_line(
                [text_block("done")], stop_reason="end_turn"))
        events, off2 = sensor.wait_once(self.jsonl, self.offset_file,
                                        interval=0.02, max_wait=5)
        self.assertEqual([e["type"] for e in events],
                         ["user", "assistant_text", "turn_end"])
        self.assertEqual(sensor.load_offset(self.offset_file), off2)

    def test_stall_flush_frees_a_partial_turn(self):
        self.jsonl.write_bytes(b"")
        sensor.save_offset(self.offset_file, 0)
        with self.jsonl.open("ab") as f:
            f.write(user_line("started") + assistant_line(
                [tool_block("Read", {"file_path": "/x.py"})]))  # no turn_end
        events, _ = sensor.wait_once(self.jsonl, self.offset_file,
                                     interval=0.02, max_wait=5,
                                     stall_flush_seconds=0.2)
        self.assertEqual(events[-1]["type"], "stall_flush")
        self.assertIn("user", [e["type"] for e in events])

    def test_dead_pid_exits_instead_of_blocking(self):
        self.jsonl.write_bytes(b"")
        sensor.save_offset(self.offset_file, 0)
        p = subprocess.Popen(["true"])
        p.wait()
        start = time.monotonic()
        events, _ = sensor.wait_once(self.jsonl, self.offset_file,
                                     pid=p.pid, interval=0.02, max_wait=10)
        self.assertLess(time.monotonic() - start, 5, "dead pid did not exit")
        self.assertEqual(events, [])

    def test_no_new_bytes_returns_empty_after_max_wait(self):
        # Negative control for the whole loop: nothing happened → nothing
        # reported, offset untouched.
        self.jsonl.write_bytes(FULL_TURN)
        sensor.save_offset(self.offset_file, self.jsonl.stat().st_size)
        events, off = sensor.wait_once(self.jsonl, self.offset_file,
                                       interval=0.02, max_wait=0.15)
        self.assertEqual(events, [])
        self.assertEqual(sensor.load_offset(self.offset_file), off)


class NudgeDeliveryHook(unittest.TestCase):
    """hooks/monitor-nudge.sh — the mechanical outbox → context seam."""

    def _project(self, marker: "str | None" = None) -> Path:
        proj = Path(tempfile.mkdtemp()) / "proj"
        (proj / ".agent" / "tasks").mkdir(parents=True)
        (proj / ".agent" / "monitor").mkdir()
        (proj / ".agent" / "chat_log.md").write_text("# Project Chat Log\n",
                                                     encoding="utf-8")
        if marker is not None:
            (proj / ".agent" / "current_user").write_text(marker, encoding="utf-8")
        return proj

    def _run(self, proj: Path, env_extra: "dict | None" = None) -> subprocess.CompletedProcess:
        env = dict(os.environ, PLAYBOOK_SESSION_ID="pid-99")
        env.pop("PLAYBOOK_ROLE", None)
        env.update(env_extra or {})
        return subprocess.run(
            [bash_or_skip(), str(NUDGE_HOOK)], cwd=proj, env=env,
            input=json.dumps({"hook_event_name": "PostToolUse"}),
            capture_output=True, text=True, timeout=60)

    def test_pending_nudge_is_delivered_consumed_and_logged(self):
        proj = self._project()
        nudge = proj / ".agent" / "monitor" / "nudge.md"
        nudge.write_text("Three orient spans since M742 without an execute.",
                         encoding="utf-8")
        r = self._run(proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("[MONITOR] Three orient spans"), ctx)
        self.assertFalse(nudge.exists(), "nudge outbox was not consumed")
        log = (proj / ".agent" / "chat_log.md").read_text(encoding="utf-8")
        self.assertIn("**[MONITOR→pid-99]**", log)
        self.assertIn("Three orient spans", log)

    def test_no_nudge_means_no_output(self):
        # THE negative control: a trajectory the monitor chose not to nudge
        # (no outbox file) injects NOTHING into the front agent's context.
        proj = self._project()
        r = self._run(proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        (proj / ".agent" / "monitor" / "nudge.md").write_text("", encoding="utf-8")
        r = self._run(proj)  # empty outbox — still nothing
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_monitor_role_never_consumes_its_own_nudge(self):
        proj = self._project()
        nudge = proj / ".agent" / "monitor" / "nudge.md"
        nudge.write_text("would be eaten", encoding="utf-8")
        r = self._run(proj, env_extra={"PLAYBOOK_ROLE": "monitor"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertTrue(nudge.exists(),
                        "the monitor's own session consumed the nudge before "
                        "the front agent could see it")

    def test_lane_nudge_delivered_root_untouched(self):
        proj = self._project(marker="alice\n")
        (proj / ".agent" / "alice" / "tasks").mkdir(parents=True)
        (proj / ".agent" / "alice" / "monitor").mkdir()
        (proj / ".agent" / "alice" / "chat_log.md").write_text("# log\n", encoding="utf-8")
        root_nudge = proj / ".agent" / "monitor" / "nudge.md"
        root_nudge.write_text("WRONG LANE", encoding="utf-8")
        (proj / ".agent" / "alice" / "monitor" / "nudge.md").write_text(
            "alice's nudge", encoding="utf-8")
        r = self._run(proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("alice's nudge", ctx)
        self.assertTrue(root_nudge.exists(), "root outbox consumed on a lane repo")

    def test_malformed_marker_delivers_nothing(self):
        proj = self._project(marker="../evil\n")
        (proj / ".agent" / "monitor" / "nudge.md").write_text("x", encoding="utf-8")
        r = self._run(proj)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "",
                         "a malformed marker must deliver nothing, not fall "
                         "back to the shared root")


class BootstrapGuards(unittest.TestCase):
    """bootstrap.sh: refuse to run blind; seed the offset at EOF."""

    def _run(self, env_extra: dict, args: "list[str]" = ()) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        for k in ("PLAYBOOK_PROJECT_DIR", "PLAYBOOK_AGENT_DIR", "PLAYBOOK_SESSION_ID"):
            env.pop(k, None)
        env.update(env_extra)
        return subprocess.run(
            [bash_or_skip(), str(MONITOR_LIB / "bootstrap.sh"), *args],
            capture_output=True, text=True, timeout=120, env=env)

    def test_refuses_without_project_dir(self):
        r = self._run({})
        self.assertEqual(r.returncode, 2)
        self.assertIn("PLAYBOOK_PROJECT_DIR", r.stderr)

    def test_refuses_without_session_id(self):
        proj = Path(tempfile.mkdtemp())
        r = self._run({"PLAYBOOK_PROJECT_DIR": str(proj),
                       "PLAYBOOK_AGENT_DIR": str(proj / ".agent")})
        self.assertEqual(r.returncode, 2)
        self.assertIn("session id", r.stderr)

    def test_rejects_shell_metacharacter_session_id(self):
        # The validation guards path traversal and sandbox-profile injection.
        proj = Path(tempfile.mkdtemp())
        r = self._run({"PLAYBOOK_PROJECT_DIR": str(proj),
                       "PLAYBOOK_AGENT_DIR": str(proj / ".agent"),
                       "PLAYBOOK_SESSION_ID": "pid-123;touch /tmp/pwned"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid SESSION_ID", r.stderr)

    def test_happy_path_emits_commands_and_seeds_offset_at_eof(self):
        home = Path(tempfile.mkdtemp()) / "home"
        proj = Path(tempfile.mkdtemp()) / "proj"
        (proj / ".agent" / "tasks").mkdir(parents=True)
        slug = str(proj).replace("/", "-")
        sess_dir = home / ".claude" / "projects" / slug
        # exist_ok: on Windows str(proj) keeps backslashes and a drive letter,
        # so `home / … / slug` reinterprets the slug as absolute and resolves
        # back onto the existing proj dir — mkdir(parents=True) then raises
        # FileExistsError (WinError 183). Harmless off Windows (fresh path).
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "front.jsonl").write_bytes(FULL_TURN)
        r = self._run({"PLAYBOOK_PROJECT_DIR": str(proj),
                       "PLAYBOOK_AGENT_DIR": str(proj / ".agent"),
                       "PLAYBOOK_SESSION_ID": "pid-424242",
                       "HOME": str(home)})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout[-2000:])
        self.assertIn("## COMMANDS", r.stdout)
        self.assertIn("--wait-once", r.stdout)
        self.assertIn("front.jsonl", r.stdout)
        offset_file = proj / ".agent" / "monitor" / ".offset"
        self.assertTrue(offset_file.exists(), "offset file not seeded")
        self.assertEqual(int(offset_file.read_text().strip()),
                         (sess_dir / "front.jsonl").stat().st_size,
                         "offset must seed at EOF so the monitor doesn't "
                         "replay the whole session")


if __name__ == "__main__":
    unittest.main()
