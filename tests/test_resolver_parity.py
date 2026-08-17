#!/usr/bin/env python3
"""`tasks doctor`'s Python≡bash session-id parity check, made hermetic (1.5.17).

The check false-FAILed when the suite/doctor ran DETACHED (no agent process on
the ancestry): the raw-pid fallback keys off each process's own getppid(), so
the Python process and the bash subprocess legitimately differ, and exact
equality was wrong to demand. `_resolver_parity_verdict` isolates the decision:
exact equality only when a real agent root exists (both resolvers converge on
it), structural (`pid-…` shape) agreement otherwise.

Run: python3 -m unittest tests.test_resolver_parity
"""
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from tasks.diagnostics import _resolver_parity_verdict  # noqa: E402


class ResolverParityVerdictTest(unittest.TestCase):
    # ── no agent root (detached / background — the hermeticity case) ──────────
    def test_no_root_different_pids_still_agree(self):
        # This is the exact scenario that false-FAILed: two process-local pids.
        ok, detail = _resolver_parity_verdict(False, "pid-1234", "pid-5678")
        self.assertTrue(ok, detail)

    def test_no_root_requires_pid_shape_on_both(self):
        self.assertFalse(_resolver_parity_verdict(False, "pid-1", "garbage")[0])
        self.assertFalse(_resolver_parity_verdict(False, "nope", "pid-1")[0])

    # ── real agent root (foreground, inside a playbook session) ───────────────
    def test_root_requires_exact_equality(self):
        ok, _ = _resolver_parity_verdict(True, "pid-999", "pid-999")
        self.assertTrue(ok)

    def test_root_mismatch_is_a_real_split_brain(self):
        # With a shared root the resolvers MUST converge; divergence is the bug
        # the guard exists to catch — must still fail loudly.
        ok, detail = _resolver_parity_verdict(True, "pid-111", "pid-222")
        self.assertFalse(ok)
        self.assertIn("MISMATCH", detail)

    def test_root_non_pid_form_fails(self):
        self.assertFalse(_resolver_parity_verdict(True, "sess-abc", "sess-abc")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
