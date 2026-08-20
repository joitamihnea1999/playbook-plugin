"""I13 (verification-report-1.5.9): NotebookEdit and MultiEdit must be gated.

hooks.json's PreToolUse matcher omitted both (and `.ipynb` was absent from the
code-file classifier), yet state-echo treats NotebookEdit as an editing tool. A
no-task code edit via those tools was never gated — a full bypass of "no code
without an active task".
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
PLUGIN = REPO_ROOT / "plugins" / "playbook"
HOOK = PLUGIN / "scripts" / "task-gate-hook"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"


class MatcherRegistration(unittest.TestCase):
    def test_pretooluse_matcher_includes_notebook_and_multiedit(self):
        data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        matcher = data["hooks"]["PreToolUse"][0]["matcher"]
        alts = set(matcher.split("|"))
        self.assertIn("NotebookEdit", alts, f"matcher omits NotebookEdit: {matcher}")
        self.assertIn("MultiEdit", alts, f"matcher omits MultiEdit: {matcher}")


class GateBehavior(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".agent" / "tasks").mkdir(parents=True)

    def _run(self, tool_name, tool_input):
        env = dict(os.environ)
        env["PLAYBOOK_SESSION_ID"] = "pid-nb-test"
        env.pop("BASH_ENV", None)
        return subprocess.run(
            [bash_or_skip(), str(HOOK)], cwd=self.project, env=env, text=True,
            input=json.dumps({"tool_name": tool_name, "tool_input": tool_input}),
            capture_output=True)

    def test_multiedit_code_file_blocked_without_task(self):
        r = self._run("MultiEdit",
                      {"file_path": str(self.project / "src" / "main.py")})
        self.assertEqual(r.returncode, 2,
                         f"MultiEdit bypassed the gate (rc={r.returncode})")

    def test_notebookedit_blocked_without_task(self):
        r = self._run("NotebookEdit",
                      {"notebook_path": str(self.project / "nb.ipynb")})
        self.assertEqual(r.returncode, 2,
                         f"NotebookEdit bypassed the gate (rc={r.returncode})")

    def test_multiedit_allowed_with_active_task(self):
        # Negative control: with an active task, MultiEdit is allowed.
        td = self.project / ".agent" / "tasks" / "001-x"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            "# 001 - x\n## Status\nin_progress\n## Work Plan\n- [ ] g\n",
            encoding="utf-8")
        sess = self.project / ".agent" / "sessions" / "pid-nb-test"
        sess.mkdir(parents=True)
        (sess / "current_state").write_text("001\n", encoding="utf-8")
        r = self._run("MultiEdit",
                      {"file_path": str(self.project / "src" / "main.py")})
        self.assertEqual(r.returncode, 0, f"MultiEdit blocked with active task: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
