#!/usr/bin/env python3
"""F19 — the monitor binds to the session's OWN transcript, not an mtime guess.

Field origin (StrataDB batch 6): the monitor announced the right pid but
tailed the newest-by-mtime jsonl in the project slug dir (bootstrap.sh:73) —
a stale conversation — and waited 40 minutes at its EOF while the real session
streamed megabytes into other files. Three legs, all covered here:

  a. hooks RECORD the session's transcript_path (hook payloads carry it) into
     `.agent/sessions/<id>/transcript_path` — exact binding, no heuristic;
  b. bootstrap prefers the pointer over any newer-mtime decoy, and says so;
     without a pointer it falls back LOUDLY;
  c. the sensor follows the pointer per invocation (`--pointer-file`) and
     resets its offset when the pointer moves to a different file (rollover),
     instead of waiting forever on a dead inode.

Run: python3 -m unittest tests.test_monitor_transcript_binding
"""
from __future__ import annotations

import json
import os
import subprocess
from tests._bashcheck import bash_or_skip
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
SENSOR = PLUGIN / "scripts" / "monitor-lib" / "sensor.py"
BOOTSTRAP = PLUGIN / "scripts" / "monitor-lib" / "bootstrap.sh"
STATE_ECHO = PLUGIN / "scripts" / "state-echo-hook"

SESSION = "pid-77777"


def _project() -> Path:
    d = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / ".agent" / "tasks").mkdir(parents=True)
    (d / "CLAUDE.md").write_text("tasks work\n", encoding="utf-8")
    return d


def _event_line(text="hi", typ="user") -> str:
    return json.dumps({"type": typ, "timestamp": "2026-08-14T10:00:00Z",
                       "message": {"role": "user", "content": text}}) + "\n"


class HookRecordsTranscript(unittest.TestCase):
    def _run_hook(self, proj: Path, payload: dict):
        env = dict(os.environ, PLAYBOOK_SESSION_ID=SESSION)
        return subprocess.run([bash_or_skip(), str(STATE_ECHO)],
                              input=json.dumps(payload), cwd=proj, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_state_echo_records_transcript_path(self):
        proj = _project()
        tp = proj / "fake-transcript.jsonl"
        self._run_hook(proj, {"tool_name": "Bash", "session_id": "x",
                              "transcript_path": str(tp),
                              "tool_input": {"command": "true"}})
        ptr = proj / ".agent" / "sessions" / SESSION / "transcript_path"
        self.assertTrue(ptr.exists(), "hook did not record transcript_path")
        self.assertEqual(ptr.read_text(encoding="utf-8").strip(), str(tp))

    def test_absent_field_writes_nothing(self):
        proj = _project()
        self._run_hook(proj, {"tool_name": "Bash",
                              "tool_input": {"command": "true"}})
        ptr = proj / ".agent" / "sessions" / SESSION / "transcript_path"
        self.assertFalse(ptr.exists(),
                         "no transcript_path in payload must write no pointer")

    def test_newline_injection_refused(self):
        proj = _project()
        self._run_hook(proj, {"tool_name": "Bash",
                              "transcript_path": "/tmp/a\n/tmp/b",
                              "tool_input": {"command": "true"}})
        ptr = proj / ".agent" / "sessions" / SESSION / "transcript_path"
        self.assertFalse(ptr.exists(), "a path with a newline must be refused")


class BootstrapBinding(unittest.TestCase):
    def _bootstrap(self, proj: Path, home: Path) -> str:
        env = dict(os.environ, HOME=str(home),
                   PLAYBOOK_SESSION_ID=SESSION,
                   PLAYBOOK_PROJECT_DIR=str(proj),
                   PLAYBOOK_AGENT_DIR=str(proj / ".agent"),
                   MONITOR_SRC=str(BOOTSTRAP.parent))
        r = subprocess.run([bash_or_skip(), str(BOOTSTRAP)], cwd=proj, env=env,
                           capture_output=True, text=True, timeout=120)
        return r.stdout + r.stderr

    def _slugdir(self, proj: Path, home: Path) -> Path:
        slug = str(proj).replace("/", "-")
        d = home / ".claude" / "projects" / slug
        # exist_ok: on Windows str(proj) keeps backslashes and a drive letter,
        # so joining `slug` reinterprets it as absolute and `d` resolves back
        # onto the existing proj dir — mkdir(parents=True) then raises
        # FileExistsError (WinError 183). Harmless off Windows (fresh path).
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_pointer_beats_newer_mtime_decoy(self):
        proj = _project()
        home = Path(tempfile.mkdtemp())
        slug = self._slugdir(proj, home)
        real = slug / "real-session.jsonl"
        real.write_text(_event_line("the real one"), encoding="utf-8")
        decoy = slug / "decoy-session.jsonl"
        decoy.write_text(_event_line("stale"), encoding="utf-8")
        os.utime(real, (1, 1))  # decoy is NEWER by mtime — the batch-6 trap
        sdir = proj / ".agent" / "sessions" / SESSION
        sdir.mkdir(parents=True)
        (sdir / "transcript_path").write_text(str(real), encoding="utf-8")
        out = self._bootstrap(proj, home)
        self.assertIn("real-session.jsonl", out)
        self.assertIn("session-bound", out)
        self.assertNotIn("decoy-session.jsonl",
                         out.split("RECENT EVENTS")[0],
                         "identity section must name the bound file, not the decoy")

    def test_no_pointer_falls_back_loudly(self):
        proj = _project()
        home = Path(tempfile.mkdtemp())
        slug = self._slugdir(proj, home)
        (slug / "only.jsonl").write_text(_event_line(), encoding="utf-8")
        (proj / ".agent" / "sessions" / SESSION).mkdir(parents=True)
        out = self._bootstrap(proj, home)
        self.assertIn("only.jsonl", out)
        self.assertIn("WARNING", out)
        self.assertIn("mtime", out.lower())

    def test_wait_command_follows_the_pointer(self):
        proj = _project()
        home = Path(tempfile.mkdtemp())
        slug = self._slugdir(proj, home)
        real = slug / "real-session.jsonl"
        real.write_text(_event_line(), encoding="utf-8")
        sdir = proj / ".agent" / "sessions" / SESSION
        sdir.mkdir(parents=True)
        (sdir / "transcript_path").write_text(str(real), encoding="utf-8")
        out = self._bootstrap(proj, home)
        self.assertIn("--pointer-file", out,
                      "the emitted WAIT COMMAND must re-resolve per wake")


class SensorFollowsPointer(unittest.TestCase):
    def _read(self, pointer: Path, offset_file: Path) -> str:
        r = subprocess.run(
            [sys.executable, str(SENSOR), "/nonexistent-positional.jsonl",
             "--pointer-file", str(pointer),
             "--offset-file", str(offset_file), "--from-start"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_pointer_overrides_positional_and_switch_resets_offset(self):
        d = Path(tempfile.mkdtemp())
        a = d / "a.jsonl"; b = d / "b.jsonl"
        a.write_text(_event_line("from A"), encoding="utf-8")
        b.write_text(_event_line("from B"), encoding="utf-8")
        ptr = d / "transcript_path"; off = d / ".offset"
        ptr.write_text(str(a), encoding="utf-8")
        out = self._read(ptr, off)
        self.assertIn("from A", out)
        # rollover: pointer moves to B — next read must start B at 0,
        # not carry A's larger offset or wait on A forever.
        ptr.write_text(str(b), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(SENSOR), "/nonexistent.jsonl",
             "--pointer-file", str(ptr), "--offset-file", str(off)],
            capture_output=True, text=True, timeout=60)
        self.assertIn("from B", r.stdout,
                      "switching the pointer must re-read the new file from 0")

    def test_same_file_offset_still_persists(self):
        # Negative control: pointer stable → offsets keep their old meaning
        # (no replay of already-seen events).
        d = Path(tempfile.mkdtemp())
        a = d / "a.jsonl"
        a.write_text(_event_line("first"), encoding="utf-8")
        ptr = d / "transcript_path"; off = d / ".offset"
        ptr.write_text(str(a), encoding="utf-8")
        self._read(ptr, off)
        with a.open("a") as f:
            f.write(_event_line("second"))
        r = subprocess.run(
            [sys.executable, str(SENSOR), str(a),
             "--pointer-file", str(ptr), "--offset-file", str(off)],
            capture_output=True, text=True, timeout=60)
        self.assertIn("second", r.stdout)
        self.assertNotIn("first", r.stdout,
                         "stable pointer must not replay consumed events")


if __name__ == "__main__":
    unittest.main()
