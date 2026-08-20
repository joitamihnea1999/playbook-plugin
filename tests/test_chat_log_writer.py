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


class ChatLogWriter(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
