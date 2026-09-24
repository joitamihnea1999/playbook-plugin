"""Writer-format test for chat-log-hook (§2.3): the chat-log writer had no
tests, and its `(provider/pid)` header suffix (added 1.4.3) is what silently
broke a downstream parser (I12). Pinning the writer's OUTPUT FORMAT means a
future drift can fail a test instead of a reader.
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
HOOK = REPO_ROOT / "plugins" / "playbook" / "scripts" / "chat-log-hook"
SID = "pid-clw"


class _ChatLogFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)
        self.log = self.project / ".agent" / "chat_log.md"

    def _run(self, prompt, provider=None):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = SID
        env.pop("BASH_ENV", None)
        if provider:
            env["PLAYBOOK_PROVIDER"] = provider
        return subprocess.run(
            [bash_or_skip(), str(HOOK)], cwd=self.project, env=env, text=True,
            input=json.dumps({"prompt": prompt}), capture_output=True)


class ChatLogWriter(_ChatLogFixture):
    def test_entry_format_and_sequence(self):
        self._run("first message")
        self._run("second message")
        text = self.log.read_text(encoding="utf-8")
        # Header format: **[MNNN]** [<ts> UTC] `HOST` (provider/sid)
        self.assertRegex(
            text,
            r"\*\*\[M001\]\*\* \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\] "
            r"`HOST` \(claude/pid-clw\)")
        self.assertIn("**[M002]**", text)
        self.assertIn("first message", text)
        self.assertIn("second message", text)

    def test_provider_suffix_reflects_env(self):
        self._run("hi", provider="codex")
        text = self.log.read_text(encoding="utf-8")
        self.assertRegex(text, r"`HOST` \(codex/pid-clw\)")


class HarnessPromptsAreNotUserWords(_ChatLogFixture):
    """PLAN S5a (task 088): a UserPromptSubmit payload that BEGINS with a harness
    marker is a harness event (a background-task notification, a slash-command
    echo), not the user's words — on 2026-09-23, 498 of 833 chat_log entries began
    with `<task-notification>`, and attribution/intent/retro read them as the user."""

    def _logged(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_task_notification_prompt_is_not_logged(self):
        r = self._run("<task-notification> <task-id>b1</task-id> probe-harness-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("probe-harness-1", self._logged())

    def test_plain_prompt_is_still_logged(self):
        self._run("plain probe-user-1")
        self.assertIn("plain probe-user-1", self._logged())
        self.assertIn("**[M001]**", self._logged())

    def test_every_harness_marker_is_skipped(self):
        for n, marker in enumerate(("<local-command-caveat>", "<command-name>", "[SYSTEM NOTIFICATION - NOT USER INPUT]",
                                    "  <task-notification>"), start=2):
            with self.subTest(marker=marker):
                self._run(f"{marker} probe-harness-{n}")
                self.assertNotIn(f"probe-harness-{n}", self._logged())

    def test_a_skipped_harness_prompt_still_resets_the_session_counters(self):
        # The skip is about the LOG only. Every prompt resets the session's
        # tools/writes counters, which the stop hook's conversational bypass
        # reads; exiting before that reset would change stop-hook behaviour.
        counters = self.project / ".agent" / "sessions" / SID / "counters"
        counters.parent.mkdir(parents=True)
        counters.write_bytes(b"tools=7\nwrites=3\ngate_x=1\n")
        r = self._run("<task-notification> probe-harness-reset")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("probe-harness-reset", self._logged())
        text = counters.read_text(encoding="utf-8")
        self.assertIn("tools=0", text)
        self.assertIn("writes=0", text)
        self.assertIn("gate_x=1", text)

    def test_a_user_prompt_that_merely_mentions_a_marker_is_logged(self):
        self._run("why do I see <task-notification> lines? probe-user-2")
        self.assertIn("probe-user-2", self._logged())


if __name__ == "__main__":
    unittest.main()
