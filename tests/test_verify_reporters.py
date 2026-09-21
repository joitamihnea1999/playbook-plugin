"""The two shell-fixture failure reporters must keep the fixture's own
diagnostic block, not just the `FAIL` line.

`tests/wrapper-multiuser-fixture.sh`'s `assert_contains` prints the captured
command output between `----- output start -----` / `----- output end -----`
right after its `FAIL` line. Until task 076 both reporters — `scripts/verify`'s
`sh_fails` (FAIL-prefixed lines only) and `tests/test_shell_fixtures.py`
(FAIL lines + the last 25 lines) — discarded that block, so two Windows S7
failures (CI runs 34381991629 and 35570688583, attempt 1 each) left zero bytes
of the wrapper's output to root-cause from. These tests pin: a FAIL followed by
a block yields the block (bounded); a FAIL without a block is unchanged.

Run: python3 -m unittest tests.test_verify_reporters
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_shell_fixtures  # noqa: E402  (the runner module under test)


def _load_verify():
    path = ROOT / "scripts" / "verify"
    loader = importlib.machinery.SourceFileLoader("verify_under_test", str(path))
    spec = importlib.util.spec_from_loader("verify_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


TRANSCRIPT_WITH_BLOCK = """\
=== S6: mixed layout ===
  PASS  S6 mixed layout still launches
=== S7: non-playbook directory still launches the bare CLI ===
  FAIL  S7 exits 0 outside a playbook project — expected [0], got [1]
  FAIL  S7 launches with an empty project root — expected to find 'launched root='
----- output start -----
Error: playbook-codex found per-user playbook lanes but no .agent/current_user marker.
  Project: /tmp
----- output end -----
=== S8: orphan wrapper ===
  PASS  S8 exits 1 when the hook library is missing
wrapper multi-user fixture: 1 passed, 2 failed
"""

TRANSCRIPT_NO_BLOCK = """\
  PASS  S1 ok
  FAIL  S2 exits 0 — expected [0], got [1]
  PASS  S3 ok
wrapper multi-user fixture: 2 passed, 1 failed
"""


class ShFails(unittest.TestCase):
    def setUp(self):
        self.verify = _load_verify()

    def test_block_after_fail_is_kept(self):
        got = self.verify.sh_fails(TRANSCRIPT_WITH_BLOCK)
        joined = "\n".join(got)
        self.assertIn("FAIL  S7 exits 0", joined)
        self.assertIn("FAIL  S7 launches", joined)
        self.assertIn("found per-user playbook lanes but no .agent/current_user", joined)
        self.assertIn("Project: /tmp", joined)
        # PASS lines and the summary are NOT failure detail.
        self.assertNotIn("PASS", joined)
        self.assertNotIn("wrapper multi-user fixture:", joined)

    def test_fail_without_block_unchanged(self):
        got = self.verify.sh_fails(TRANSCRIPT_NO_BLOCK)
        self.assertEqual(got, ["FAIL  S2 exits 0 — expected [0], got [1]"])

    def test_block_is_bounded(self):
        big = "  FAIL  X\n----- output start -----\n" + "\n".join(
            f"line {i}" for i in range(200)) + "\n----- output end -----\n"
        got = self.verify.sh_fails(big)
        # One FAIL + a bounded slice of the block + a remainder marker, never 200 lines.
        self.assertLess(len(got), 60)   # 1 FAIL + BLOCK_CAP (40) + remainder, never 200
        self.assertTrue(any("line 0" in ln for ln in got))
        self.assertTrue(any("more" in ln for ln in got), got)


# A transcript long enough that the last-25 tail does NOT already contain the
# block (076 impl panel, grok #3: the short transcript above stayed green on the
# pre-076 reporter because the tail happened to include the block).
TRANSCRIPT_LONG = TRANSCRIPT_WITH_BLOCK + "\n".join(
    f"  PASS  S{i} later scenario" for i in range(8, 60)) + "\nwrapper multi-user fixture: 52 passed, 2 failed\n"

# One large block, then ten more failing assertions (076 impl panel, codex-medium
# #2: the old overall cap kept only six of the ten).
TRANSCRIPT_BIG_THEN_FAILS = (
    "  FAIL  S1 first\n----- output start -----\n" + "\n".join(f"noise {i}" for i in range(60))
    + "\n----- output end -----\n" + "\n".join(f"  FAIL  S{i} later failure {i}" for i in range(2, 12)) + "\n")

# Two blocks after one failure group (assert_contains' block, then S7's own).
TRANSCRIPT_TWO_BLOCKS = (
    "  FAIL  S7 a\n  FAIL  S7 b\n----- output start -----\nwrapper said X\n----- output end -----\n"
    "----- output start -----\nS7 diagnostics (task 076)\nrc=1\nfind_project_root from run dir: []\n----- output end -----\n")

# A line that merely CONTAINS "FAIL" mid-string is not a failing assertion.
TRANSCRIPT_MIDLINE_FAIL = "  PASS  S1 env var PLAYBOOK_FAILSAFE unset\n  FAIL  S2 real\n"

# The unittest-wrapped path (the 074 shape): traceback + assertion message that
# CARRIES the reporter's detail with a block. ut_fails must not slice the block
# away at its per-failure cap (076 impl panel, grok #1 / codex-high #1).
UT_TRANSCRIPT = (
    "======================================================================\n"
    "FAIL: test_shell_fixtures_pass (test_shell_fixtures.ShellFixtures) (fixture='wrapper-multiuser-fixture.sh')\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    "  File \"tests/test_shell_fixtures.py\", line 99, in test_shell_fixtures_pass\n"
    "    self.fail(f\"{name} failed (rc={r.returncode}):\\n{_failure_detail(lines)}\")\n"
    "AssertionError: wrapper-multiuser-fixture.sh failed (rc=1):\n"
    "  FAIL  S7 exits 0 outside a playbook project — expected [0], got [1]\n"
    "  FAIL  S7 launches with an empty project root — expected to find 'launched root='\n"
    + "\n".join(f"    | diag line {i}" for i in range(30)) + "\n"
    "    | ancestor /tmp: .agent EXISTS\n"
    "--- last 25 lines ---\n" + "\n".join(f"  PASS  S{i} x" for i in range(25)) + "\n"
    "\n----------------------------------------------------------------------\n"
    "Ran 2394 tests in 400.0s\n\nFAILED (failures=1)\n")


class ShFailsMore(unittest.TestCase):
    def setUp(self):
        self.verify = _load_verify()

    def test_every_fail_line_survives_a_big_block(self):
        got = self.verify.sh_fails(TRANSCRIPT_BIG_THEN_FAILS)
        for i in range(2, 12):
            self.assertTrue(any(f"S{i} later failure" in ln for ln in got), (i, got))

    def test_two_blocks_both_kept(self):
        got = "\n".join(self.verify.sh_fails(TRANSCRIPT_TWO_BLOCKS))
        self.assertIn("wrapper said X", got)
        self.assertIn("find_project_root from run dir", got)

    def test_midline_fail_is_not_a_failure(self):
        got = self.verify.sh_fails(TRANSCRIPT_MIDLINE_FAIL)
        self.assertEqual(got, ["FAIL  S2 real"])


class UtFailsKeepsBlock(unittest.TestCase):
    def test_block_lines_survive_the_per_failure_cap(self):
        verify = _load_verify()
        got = "\n".join(verify.ut_fails(UT_TRANSCRIPT))
        self.assertIn("FAIL  S7 exits 0", got)
        self.assertIn("diag line 29", got)
        self.assertIn("ancestor /tmp: .agent EXISTS", got)
        # The 25-line PASS tail is still budgeted away — only the block is exempt.
        self.assertNotIn("PASS  S24 x", got)


class ShellFixturesDetail(unittest.TestCase):
    def test_block_after_fail_is_kept(self):
        detail = test_shell_fixtures._failure_detail(TRANSCRIPT_WITH_BLOCK.splitlines())
        self.assertIn("FAIL  S7 exits 0", detail)
        self.assertIn("found per-user playbook lanes but no .agent/current_user", detail)
        self.assertIn("--- last 25 lines ---", detail)

    def test_block_precedes_the_tail_on_a_long_transcript(self):
        detail = test_shell_fixtures._failure_detail(TRANSCRIPT_LONG.splitlines())
        head = detail.split("--- last 25 lines ---")[0]
        self.assertIn("found per-user playbook lanes but no .agent/current_user", head)

    def test_block_is_bounded(self):
        big = ["  FAIL  X", "----- output start -----"] + [f"line {i}" for i in range(200)] + ["----- output end -----"]
        detail = test_shell_fixtures._failure_detail(big)
        head = detail.split("--- last 25 lines ---")[0]
        self.assertLess(len(head.splitlines()), 60)
        self.assertIn("more output line", head)

    def test_every_fail_line_survives_a_big_block(self):
        detail = test_shell_fixtures._failure_detail(TRANSCRIPT_BIG_THEN_FAILS.splitlines())
        head = detail.split("--- last 25 lines ---")[0]
        for i in range(2, 12):
            self.assertIn(f"S{i} later failure", head)

    def test_midline_fail_is_not_a_failure(self):
        detail = test_shell_fixtures._failure_detail(TRANSCRIPT_MIDLINE_FAIL.splitlines())
        head = detail.split("--- last 25 lines ---")[0]
        self.assertNotIn("PLAYBOOK_FAILSAFE", head)
        self.assertIn("FAIL  S2 real", head)

    def test_fail_without_block_unchanged(self):
        detail = test_shell_fixtures._failure_detail(TRANSCRIPT_NO_BLOCK.splitlines())
        self.assertTrue(detail.startswith("  FAIL  S2 exits 0"), detail)


class ForcedS7EndToEnd(unittest.TestCase):
    """Run the REAL fixture with its test-only knob that forces S7's exit
    assertion to fail while the shim still prints `launched root=` (the
    concrete mode codex-medium #1 named: rc != 0 but the content assertion
    passes). Both reporters must then carry the diagnostics AND the wrapper's
    output. Proves the fixture actually produces the block (grok #4)."""

    def test_diagnostics_reach_both_reporters(self):
        import os, shutil, subprocess, tempfile
        if shutil.which("bash") is None or shutil.which("git") is None:
            self.skipTest("bash/git missing")
        env = test_shell_fixtures._clean_env()
        env["WRAPPER_FIXTURE_FORCE_S7_RC"] = "1"
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([test_shell_fixtures.bash_or_skip(), str(ROOT / "tests" / "wrapper-multiuser-fixture.sh")],
                               cwd=td, env=env, capture_output=True, text=True, timeout=600)
        self.assertNotEqual(r.returncode, 0)
        transcript = r.stdout + r.stderr
        for reporter in (lambda t: "\n".join(_load_verify().sh_fails(t)),
                         lambda t: test_shell_fixtures._failure_detail(t.splitlines()).split("--- last 25 lines ---")[0]):
            got = reporter(transcript)
            self.assertIn("S7 exits 0 outside a playbook project", got)
            self.assertIn("S7 diagnostics (task 076)", got)
            self.assertIn("find_project_root from run dir: []", got)
            self.assertIn("ancestor", got)
            self.assertIn("launched root=", got)          # the wrapper's own output is inside the block


if __name__ == "__main__":
    unittest.main()
