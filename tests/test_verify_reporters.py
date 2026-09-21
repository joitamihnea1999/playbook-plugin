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


class ShellFixturesDetail(unittest.TestCase):
    def test_block_after_fail_is_kept(self):
        detail = test_shell_fixtures._failure_detail(TRANSCRIPT_WITH_BLOCK.splitlines())
        self.assertIn("FAIL  S7 exits 0", detail)
        self.assertIn("found per-user playbook lanes but no .agent/current_user", detail)
        self.assertIn("--- last 25 lines ---", detail)

    def test_fail_without_block_unchanged(self):
        detail = test_shell_fixtures._failure_detail(TRANSCRIPT_NO_BLOCK.splitlines())
        self.assertTrue(detail.startswith("  FAIL  S2 exits 0"), detail)


if __name__ == "__main__":
    unittest.main()
