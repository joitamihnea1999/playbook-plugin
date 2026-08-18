"""I4 (verification-report-1.5.9): `scripts/init` must not abort mid-install.

Two vectors:
  * `MERGE_OUT=$(python3 claude-md-merge.py …)` at init:419 had no `|| {…}`
    guard, and `set -e` aborts on a bare assignment from a failing command
    substitution — so a CLAUDE.md merge failure killed init before the graceful
    FAILED handler and the summary, leaving a half-configured project.
  * Embedded-python paths were interpolated into the source as `'$PATH'`, so a
    project path containing a single quote (`~/John's proj`) made python raise
    a SyntaxError (uncatchable by the in-script try/except) → non-zero exit →
    `set -e` abort mid-install.

The fix guards the merge call and passes every path/command to embedded python
via the environment (never string interpolation), so init runs to its summary.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT = REPO_ROOT / "plugins" / "playbook" / "scripts" / "init"


class InitRobustness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def _run_init(self, project: Path, name="proj"):
        project.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["HOME"] = str(self.home)  # never touch the real ~/.claude
        return subprocess.run(
            ["bash", str(INIT), name],
            cwd=project, env=env, text=True, capture_output=True,
        )

    def test_init_completes_with_single_quote_path(self):
        project = Path(self._tmp.name) / "John's proj"
        r = self._run_init(project)
        # It must reach the summary (never abort mid-install)...
        self.assertIn("playbook init:", r.stdout,
                      f"init aborted mid-install (rc={r.returncode}): {r.stderr}")
        # ...and exit clean with the doctrine file written.
        self.assertEqual(r.returncode, 0,
                         f"init failed on a single-quote path: {r.stdout}\n{r.stderr}")
        self.assertTrue((project / "CLAUDE.md").exists(),
                        "CLAUDE.md not written on a single-quote path")

    def test_init_reaches_summary_when_merge_fails(self):
        # Force claude-md-merge.py to fail: make CLAUDE.md an (unwritable-as-file)
        # directory. init must still reach its summary, not abort at :419.
        project = Path(self._tmp.name) / "mergefail"
        project.mkdir(parents=True)
        (project / "CLAUDE.md").mkdir()  # a dir where a file is expected
        r = self._run_init(project)
        self.assertIn("playbook init:", r.stdout,
                      f"init aborted at the merge step (rc={r.returncode}): {r.stderr}")

    def test_init_clean_path_is_clean(self):
        # Negative control: a plain path initializes with exit 0.
        project = Path(self._tmp.name) / "plain"
        r = self._run_init(project)
        self.assertEqual(r.returncode, 0, f"clean init failed: {r.stdout}\n{r.stderr}")
        self.assertTrue((project / "CLAUDE.md").exists())

    def test_seeds_risk_gated_close_policy(self):
        # 1.5.28: the seeded default is risk-gated, NOT "all" — reversible work
        # (the common case) must close without a panel; only assertive/irreversible
        # gate on one. A drift back to "all" would silently re-impose a panel on
        # every trivial close.
        import json
        project = Path(self._tmp.name) / "policy"
        r = self._run_init(project)
        self.assertEqual(r.returncode, 0, f"init failed: {r.stdout}\n{r.stderr}")
        cfg = json.loads((project / ".agent" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["panel_required_for"], ["assertive", "irreversible"],
                         f"seeded close policy drifted: {cfg.get('panel_required_for')!r}")


if __name__ == "__main__":
    unittest.main()
