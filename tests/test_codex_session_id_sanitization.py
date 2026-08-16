"""N1 (verification-report-1.5.10): the codex resolver must sanitize the session
id too — C4's fix covered only the bash + tasks.core resolvers, but
`provider/codex_hooks.resolve_session_id` returned `PLAYBOOK_SESSION_ID`
verbatim, reached on EVERY codex user prompt, and composes hook paths that are
WRITTEN (`counters`, turn-baseline, stop-marker). `PLAYBOOK_SESSION_ID=../tasks`
made those writes escape `sessions/` into the task dir — a path-traversal write
primitive (the codex twin of C4). This makes the "one shared resolver" claim
true.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"
sys.path.insert(0, str(PLUGIN))
from provider import codex_hooks  # noqa: E402


class CodexSessionIdSanitization(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("PLAYBOOK_SESSION_ID", "CODEX_THREAD_ID")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _resolve(self, playbook=None, thread=None):
        for k, v in (("PLAYBOOK_SESSION_ID", playbook), ("CODEX_THREAD_ID", thread)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return codex_hooks.resolve_session_id()

    def test_traversal_is_neutralized(self):
        sid = self._resolve(playbook="../tasks/001-precious")
        self.assertNotIn("/", sid, f"codex resolver returned a traversal id: {sid!r}")
        self.assertNotIn("..", sid, f"codex resolver returned '..': {sid!r}")

    def test_traversal_thread_id_is_neutralized(self):
        sid = self._resolve(thread="../../evil")
        self.assertNotIn("/", sid)

    def test_composed_counter_path_stays_under_sessions(self):
        # The concrete N1 write primitive: the counters path must not escape
        # sessions/ when the env id is a traversal.
        with tempfile.TemporaryDirectory() as t:
            proj = Path(t)
            (proj / ".agent" / "sessions").mkdir(parents=True)
            (proj / ".agent" / "tasks" / "001-precious").mkdir(parents=True)
            sid = self._resolve(playbook="../tasks/001-precious")
            counters = codex_hooks._session_counter_path(proj, sid).resolve()
            sessions = (proj / ".agent" / "sessions").resolve()
            self.assertTrue(str(counters).startswith(str(sessions) + os.sep),
                            f"counter path escaped sessions/: {counters}")

    def test_valid_ids_pass(self):
        # Negative controls: legitimate ids are unchanged.
        self.assertEqual(self._resolve(playbook="pid-12345"), "pid-12345")
        self.assertEqual(self._resolve(playbook="judge"), "judge")
        # A native CODEX_THREAD_ID (uuid-ish) survives.
        self.assertEqual(self._resolve(thread="abc-123-def"), "abc-123-def")

    def test_falls_back_to_pid_when_env_is_hostile(self):
        sid = self._resolve(playbook="../x", thread="../y")
        self.assertTrue(sid.startswith("pid-"), f"no safe fallback: {sid!r}")


if __name__ == "__main__":
    unittest.main()
