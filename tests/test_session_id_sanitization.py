"""C4 (verification-report-1.5.9): the session id must be sanitized in the ONE
resolver every hook shares, BEFORE it becomes a path component.

`resolve_session_id` returned `PLAYBOOK_SESSION_ID` verbatim with zero
validation, and `session-end-hook` runs `rm -rf "$AGENT_DIR/sessions/$SESSION_ID"`.
`PLAYBOOK_SESSION_ID=../tasks` + stdin `{"reason":"logout"}` deleted
`.agent/tasks/` — the task database. The same value is a path component in
EVERY hook. Reachable both adversarially (a prompt-injected agent can `export`
it; the docs propagate it via BASH_ENV) and accidentally (any `/` or `..`).

The fix accepts only a safe single directory component (the canonical `pid-*`
ids and the sanctioned `judge` session id), and NEUTRALIZES anything else by
falling back to the derived pid.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"
SCRIPTS = PLUGIN / "scripts"

sys.path.insert(0, str(PLUGIN))
import tasks.core as core  # noqa: E402


class SessionIdResolverUnit(unittest.TestCase):
    """The shared Python resolver neutralizes an unsafe env value."""

    def setUp(self):
        self._saved = os.environ.get("PLAYBOOK_SESSION_ID")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("PLAYBOOK_SESSION_ID", None)
        else:
            os.environ["PLAYBOOK_SESSION_ID"] = self._saved

    def _resolve(self, value):
        os.environ["PLAYBOOK_SESSION_ID"] = value
        return core.resolve_session_id()

    def test_traversal_is_neutralized(self):
        sid = self._resolve("../tasks")
        self.assertNotEqual(sid, "../tasks",
                            "traversal session id returned verbatim")
        self.assertNotIn("/", sid, f"resolved id has a slash: {sid!r}")
        self.assertNotIn("..", sid, f"resolved id has '..': {sid!r}")

    def test_dotdot_is_neutralized(self):
        self.assertNotEqual(self._resolve(".."), "..")

    def test_slash_absolute_is_neutralized(self):
        sid = self._resolve("/etc")
        self.assertNotIn("/", sid)

    def test_valid_pid_id_passes(self):
        self.assertEqual(self._resolve("pid-12345"), "pid-12345")

    def test_judge_session_id_passes(self):
        # Sanctioned special value — review.py sets it, task-gate keys on it.
        self.assertEqual(self._resolve("judge"), "judge")


class SessionEndHookIntegration(unittest.TestCase):
    """The whole C4 repro end-to-end through the real bash hook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks" / "001-precious").mkdir(parents=True)
        (self.project / ".agent" / "tasks" / "001-precious" / "task.md").write_text(
            "# 001 - precious\n", encoding="utf-8")
        # The `sessions/` dir must exist for `sessions/../tasks` to resolve —
        # exactly the shape the report's repro had.
        (self.project / ".agent" / "sessions").mkdir(parents=True)

    def _run_hook(self, session_id, reason="logout"):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = session_id
        env["PATH"] = os.environ.get("PATH", "")
        return subprocess.run(
            [bash_or_skip(), str(SCRIPTS / "session-end-hook")],
            cwd=self.project, env=env, text=True,
            input=json.dumps({"reason": reason}),
            capture_output=True,
        )

    def test_traversal_session_id_does_not_delete_task_db(self):
        tasks_dir = self.project / ".agent" / "tasks"
        self.assertTrue(tasks_dir.exists())
        self._run_hook("../tasks")
        self.assertTrue(tasks_dir.exists(),
                        "session-end-hook deleted the task DB via ../tasks (C4)")
        self.assertTrue((tasks_dir / "001-precious").exists())

    def test_valid_session_dir_still_cleaned_on_logout(self):
        # Negative control: a legitimate session dir IS removed on a terminal
        # reason — the sanitization must not break normal cleanup.
        sess = self.project / ".agent" / "sessions" / "pid-99999"
        sess.mkdir(parents=True)
        (sess / "current_state").write_text("001\n", encoding="utf-8")
        self._run_hook("pid-99999", reason="logout")
        self.assertFalse(sess.exists(),
                         "valid session dir was not cleaned up on logout")


if __name__ == "__main__":
    unittest.main()
