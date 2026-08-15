"""C5 (verification-report-1.5.9): counter/offset files under .agent/ are
untrusted, so bash arithmetic on their raw bytes is command injection.

`state-echo-hook` computed `$(( ${TOOLS_VAL:-0} + 1 ))` where `TOOLS_VAL` is read
from `.agent/sessions/<id>/counters`, and bash arithmetic evaluates command
substitution inside `$(( ))`. A counters file containing
`tools=x[$(touch PWNED)]` executed `touch` on the next PostToolUse. The counters
file lives under `.agent/`, which the task-gate EXEMPTS from the code-edit gate,
so an agent (or prompt injection) can write it and achieve arbitrary command
execution in the hook context. The same class exists in `chat-log-hook`'s
`get_next_id` (`$(( n + 1 ))` on the raw `chat_log_counter`).

Fix: read counters through a strict integer coercion (`_safe_int`), never feed
raw file bytes to `$(( ))`. These tests drive the REAL hooks with a poisoned
counter and assert the payload stays inert (plus controls that real integers
still increment).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"
SCRIPTS = PLUGIN / "scripts"
SID = "pid-ctest"


class CounterInjectionBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks" / "001-x").mkdir(parents=True)
        (self.project / ".agent" / "tasks" / "001-x" / "task.md").write_text(
            "# 001 - x\n## Status\nin_progress\n## Work Plan\n- [ ] a gate\n",
            encoding="utf-8")
        self.session_dir = self.project / ".agent" / "sessions" / SID
        self.session_dir.mkdir(parents=True)
        self.sentinel = Path(self._tmp.name) / "PWNED"

    def _run(self, hook, stdin_obj):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = SID
        return subprocess.run(
            ["bash", str(SCRIPTS / hook)],
            cwd=self.project, env=env, text=True,
            input=json.dumps(stdin_obj), capture_output=True,
        )


class StateEchoCounterInjection(CounterInjectionBase):
    def test_poisoned_tools_counter_is_inert(self):
        payload = f"tools=x[$(touch {self.sentinel})]\n"
        (self.session_dir / "counters").write_text(payload, encoding="utf-8")
        self._run("state-echo-hook", {"tool_name": "Read"})
        self.assertFalse(self.sentinel.exists(),
                         "counter injection executed via state-echo-hook (C5)")

    def test_poisoned_gate_count_is_inert(self):
        # gate_count feeds `$((PREV_COUNT + 1))`.
        (self.session_dir / "current_state").write_text("001\n", encoding="utf-8")
        payload = (f"gate_key=001:5\ngate_count=x[$(touch {self.sentinel})]\n"
                   "gate_text=old\n")
        (self.session_dir / "counters").write_text(payload, encoding="utf-8")
        self._run("state-echo-hook", {"tool_name": "Read"})
        self.assertFalse(self.sentinel.exists(),
                         "gate_count injection executed (C5)")

    def test_real_integer_counter_still_increments(self):
        # Negative control: a legitimate integer counter must still advance.
        (self.session_dir / "counters").write_text("tools=5\n", encoding="utf-8")
        self._run("state-echo-hook", {"tool_name": "Read"})
        text = (self.session_dir / "counters").read_text(encoding="utf-8")
        self.assertIn("tools=6", text,
                      f"real counter did not increment: {text!r}")


class ChatLogCounterInjection(CounterInjectionBase):
    def test_poisoned_chat_log_counter_is_inert(self):
        agent = self.project / ".agent"
        (agent / "chat_log_counter").write_text(
            f"x[$(touch {self.sentinel})]\n", encoding="utf-8")
        self._run("chat-log-hook", {"prompt": "hello world"})
        self.assertFalse(self.sentinel.exists(),
                         "chat_log_counter injection executed (C5)")


if __name__ == "__main__":
    unittest.main()
