#!/usr/bin/env python3
"""Self-test for the arena replay engine (Tier 1).

Dev-tool test — run it directly (`python3 arena/test_replay.py`); it is not part
of the shipped-plugin suite under tests/. Proves the replay pipeline runs, the
shipped fixtures return the right decisions, and a real historical change is
detected (the negative control — a detector that can't see its own target is
worthless).

Requires a git repo with history (uses `git archive`).
"""
import subprocess
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import replay  # noqa: E402

FIXTURES = _HERE / "fixtures"
# Commit that first gated MultiEdit/NotebookEdit (1.5.10 I13); the commit before
# it ALLOWed MultiEdit, so replaying against it must flip fixture 05.
_I13 = "2277053"


def _has_ref(ref: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                          cwd=str(replay._REPO), capture_output=True).returncode == 0


class ReplayPipeline(unittest.TestCase):
    def test_head_vs_head_has_no_delta(self):
        out = replay.run("HEAD", FIXTURES)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["changed"], [], "HEAD vs working tree should be identical")

    def test_fixtures_return_expected_verdicts(self):
        out = replay.run("HEAD", FIXTURES)
        verdicts = {r["name"].split(" →")[0].split(" (")[0]: r["working"]["verdict"]
                    for r in out["results"]}
        self.assertEqual(verdicts["edit-code-without-active-task"], "BLOCK")
        self.assertEqual(verdicts["edit-code-with-active-task"], "ALLOW")
        self.assertEqual(verdicts["edit-doc-without-task"], "ALLOW")
        self.assertEqual(verdicts["edit-.agent-file-without-task"], "ALLOW")
        self.assertEqual(verdicts["MultiEdit-code-without-task"], "BLOCK")

    def test_detects_a_real_historical_change(self):
        # Negative control: against the pre-MultiEdit-gating baseline, exactly the
        # MultiEdit fixture must flip ALLOW→BLOCK. If nothing changes, the detector
        # is blind.
        if not _has_ref(f"{_I13}~1"):
            self.skipTest(f"baseline ref {_I13}~1 not in history")
        out = replay.run(f"{_I13}~1", FIXTURES)
        changed = out["changed"]
        self.assertEqual(len(changed), 1, f"expected exactly the MultiEdit flip, got {changed}")
        self.assertIn("MultiEdit", changed[0])
        multi = next(r for r in out["results"] if "MultiEdit" in r["name"])
        self.assertEqual(multi["baseline"]["verdict"], "ALLOW")
        self.assertEqual(multi["working"]["verdict"], "BLOCK")


class TreatmentRefComparison(unittest.TestCase):
    def test_ref_vs_ref_detects_the_flip(self):
        # baseline = before MultiEdit gating, treatment = the I13 commit itself.
        if not (_has_ref(f"{_I13}~1") and _has_ref(_I13)):
            self.skipTest("I13 refs not in history")
        out = replay.run(f"{_I13}~1", FIXTURES, treatment_ref=_I13)
        self.assertEqual(len(out["changed"]), 1)
        self.assertIn("MultiEdit", out["changed"][0])


class PrePushHook(unittest.TestCase):
    HOOK = _HERE / "githooks" / "pre-push"

    def _run_hook(self, pushed: str, remote: str):
        line = f"refs/heads/x {pushed} refs/heads/x {remote}\n"
        return subprocess.run(["bash", str(self.HOOK)], input=line,
                              cwd=str(replay._REPO), capture_output=True, text=True)

    def test_triggers_and_flags_on_a_harness_change(self):
        if not _has_ref(_I13):
            self.skipTest("I13 not in history")
        before = subprocess.run(["git", "rev-parse", f"{_I13}~1"], cwd=str(replay._REPO),
                                capture_output=True, text=True).stdout.strip()
        pushed = subprocess.run(["git", "rev-parse", _I13], cwd=str(replay._REPO),
                                capture_output=True, text=True).stdout.strip()
        r = self._run_hook(pushed, before)
        self.assertEqual(r.returncode, 0, "advisory hook must never fail the push")
        self.assertIn("harness files changed", r.stderr)
        self.assertIn("behavioral delta detected", r.stderr)

    def test_skips_when_no_harness_file_changed(self):
        # A docs-only commit range must not trigger a replay.
        if not _has_ref("fd845f9"):
            self.skipTest("docs commit not in history")
        pushed = subprocess.run(["git", "rev-parse", "fd845f9"], cwd=str(replay._REPO),
                                capture_output=True, text=True).stdout.strip()
        before = subprocess.run(["git", "rev-parse", "fd845f9~1"], cwd=str(replay._REPO),
                                capture_output=True, text=True).stdout.strip()
        r = self._run_hook(pushed, before)
        self.assertEqual(r.returncode, 0)
        self.assertIn("no harness changes", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
