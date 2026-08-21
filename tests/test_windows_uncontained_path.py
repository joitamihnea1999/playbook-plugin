#!/usr/bin/env python3
"""Windows has NO OS containment backend — pin that the judge/sandbox path there
is the UNCONTAINED one.

Verified fact: Windows ships neither `sandbox-exec` (macOS-only) nor `bwrap`
(Linux-only), so `_wrapped_argv` falls through to a direct exec with bypass
flags and no kernel write-denial, and `containment_available()` returns False.
`tasks/review.py` gates its "⚠ judge(s) running UNCONTAINED" warning on exactly
that `containment_available()` switch — so pinning it False on Windows pins that
the warning path is the one Windows takes.

Run: python3 tests/test_windows_uncontained_path.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from provider import sandbox  # noqa: E402


class WindowsTakesUncontainedPath(unittest.TestCase):
    def _windows(self, which):
        return (
            mock.patch.object(sandbox, "is_sandboxed", return_value=False),
            mock.patch.object(sandbox.platform, "system", return_value="Windows"),
            mock.patch.object(sandbox.shutil, "which", side_effect=which),
        )

    def test_windows_reports_no_containment(self):
        a, b, c = self._windows(lambda name: None)  # no sandbox-exec, no bwrap
        with a, b, c:
            self.assertFalse(sandbox.containment_available(),
                             "Windows has no seatbelt/bwrap — must report uncontained")

    def test_windows_argv_is_unwrapped_direct_exec(self):
        a, b, c = self._windows(lambda name: None)
        with a, b, c:
            argv = sandbox._wrapped_argv("claude", ["-p", "hi"], Path("/proj"),
                                         None, False)
        # Exactly the bypass-injected inner argv — no bwrap/sandbox-exec wrapper.
        self.assertEqual(argv, sandbox._compose_agent_argv("claude", ["-p", "hi"]))
        self.assertNotIn("bwrap", argv)
        self.assertNotIn("sandbox-exec", argv)

    def test_negative_control_bwrap_would_contain(self):
        """The False above is because no backend exists, not vacuously: hand the
        same code a bwrap on PATH and containment flips to True."""
        a, b, c = self._windows(
            lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
        with a, b, c:
            self.assertTrue(sandbox.containment_available())


if __name__ == "__main__":
    unittest.main(verbosity=2)
