"""Regression: the review arms must survive a TRIMMED task.md (1.5.9 judge F1).

`main()` contains `import re` statements in several arms, which makes `re` a
local of the whole function — so a bare `re.search` in a DIFFERENT arm is an
UnboundLocalError on any path where no local import ran first. Both review
arms hit exactly that when `select_task_context` trims an oversized task.md
and the trim-notice code reaches for `re.search` (panel-review inside its
nested `_build_payload`; plan/impl-review at its context build). The path was
untested: every fixture task.md fit the budget, so the receipts machinery
shipped in 1.5.3 with a crash on the very case it exists for.

These tests build a task.md too big for the transport budget, strip judge
binaries off PATH, and drive the real CLI: the run must get PAST context
building (proving the trim fired — the context receipt line is asserted, so a
too-small fixture cannot go vacuously green) and die on the ordinary
"no judge available" error instead of a traceback.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLAYBOOK = _HERE.parent / "plugins" / "playbook"


def _big_task_md() -> str:
    parts = [
        "# 001 - Big\n\n## Status\nin_progress\n\n## Intent\nhuge on purpose\n",
        "\n## Work Plan\n- [ ] a gate\n",
    ]
    for i in range(40):
        parts.append(f"\n## Filler Section {i}\n" + ("x" * 120 + "\n") * 34)
    return "".join(parts)  # ~165k chars — over both transport budgets' halves


class TrimmedContextDoesNotCrash(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        proj = Path(self._tmp.name)
        tdir = proj / ".agent" / "tasks" / "001-big"
        tdir.mkdir(parents=True)
        (tdir / "task.md").write_text(_big_task_md(), encoding="utf-8")
        (proj / "MIND_MAP.md").write_text("# Map\n[1] one node\n", encoding="utf-8")
        self.proj = proj
        empty_bin = proj / "empty-bin"
        empty_bin.mkdir()
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(_PLAYBOOK)
        self.env["PATH"] = str(empty_bin)  # no judge CLI resolvable
        self.env["PLAYBOOK_SESSION_ID"] = "pid-999999998"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", *args],
            cwd=self.proj, env=self.env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )

    def _assert_clean_failure(self, r, expected_error):
        combined = r.stdout + r.stderr
        self.assertNotIn("UnboundLocalError", combined, combined)
        self.assertNotIn("Traceback", combined, combined)
        # The trigger must actually have fired: a context receipt names the trim.
        self.assertIn("context", combined)
        self.assertIn("→", combined)  # the "N → M chars" receipt arrow
        self.assertIn(expected_error, combined)
        self.assertEqual(r.returncode, 1)

    def test_panel_review_trimmed_task(self):
        r = self._run("panel-review", "1", "--mode", "plan")
        self._assert_clean_failure(r, "no available judges")

    def test_single_review_trimmed_task(self):
        r = self._run("plan-review", "1", "--backend", "codex")
        self._assert_clean_failure(r, "not found on PATH")


if __name__ == "__main__":
    unittest.main()
