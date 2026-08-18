#!/usr/bin/env python3
"""POSIX argv per-element byte guard (upstream issue #10).

Context is budgeted in CHARACTERS but grok/agy/pi deliver it in one argv element
capped at MAX_ARG_STRLEN = 32*PAGE_SIZE BYTES. A char budget cannot bound a byte
channel: past ~1.29 B/char a 100k-char context overflows execve with a cryptic
E2BIG. The existing guard was Windows-only, counted chars, and summed the whole
line. This pins the POSIX half.

Invariants (from the write-up):
  * BYTES decide, not chars — a multibyte context under a char budget but over the
    byte limit is rejected;
  * MAX over elements, not SUM — many small args over the total pass; one oversized
    element fails;
  * the limit is derived from PAGE_SIZE, not hardcoded 131,072;
  * parity: grok, agy and pi all refuse an oversized context the same way;
  * stdin adapters (claude, codex) do not import the guard — they are unaffected.

Pure stdlib unittest. Run: python3 tests/test_argv_byte_guard.py
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))
from provider.argv_guard import argv_byte_error, max_arg_bytes  # noqa: E402

LIMIT = max_arg_bytes()


class MaxArgBytes(unittest.TestCase):
    def test_derived_from_page_size(self):
        if hasattr(os, "sysconf"):
            self.assertEqual(max_arg_bytes(), 32 * os.sysconf("SC_PAGESIZE"))
        else:
            self.assertEqual(max_arg_bytes(), 32 * 4096)


@unittest.skipIf(os.name == "nt", "POSIX-only guard; Windows arm is separate")
class ArgvByteError(unittest.TestCase):
    def test_boundary_is_exact(self):
        self.assertIsNone(argv_byte_error(["x" * (LIMIT - 1)], "grok"))
        self.assertIsNotNone(argv_byte_error(["x" * LIMIT], "grok"))

    def test_bytes_not_chars(self):
        # Half as many CHARS as the limit, but 2 bytes each → over the BYTE limit.
        multibyte = "ă" * (LIMIT // 2 + 1)          # each 'ă' is 2 UTF-8 bytes
        self.assertLess(len(multibyte), LIMIT)       # under a char budget
        self.assertGreater(len(multibyte.encode()), LIMIT)
        self.assertIsNotNone(argv_byte_error([multibyte], "grok"))

    def test_max_not_sum(self):
        # Many small args far over the TOTAL, but none oversized → fine (per-element).
        many = ["x" * 1000] * (LIMIT // 100)
        self.assertGreater(sum(len(a) for a in many), LIMIT * 3)
        self.assertIsNone(argv_byte_error(many, "grok"))
        # One oversized element among small ones → fails.
        self.assertIsNotNone(argv_byte_error(["ok", "x" * LIMIT, "ok"], "grok"))

    def test_empty_is_fine(self):
        self.assertIsNone(argv_byte_error([], "grok"))

    def test_message_is_actionable(self):
        msg = argv_byte_error(["x" * LIMIT], "grok")
        self.assertIn("bytes", msg)
        self.assertIn("MAX_ARG_STRLEN", msg)
        self.assertIn("stdin-capable", msg)
        self.assertIn("grok", msg)


@unittest.skipIf(os.name == "nt", "POSIX-only guard")
class AdapterParity(unittest.TestCase):
    """grok, agy, pi all refuse an oversized context before spawning."""

    def _adapters(self):
        from provider.adapters.grok import GrokAdapter
        from provider.adapters.antigravity import AntigravityAdapter
        from provider.adapters.pi import PiAdapter
        return [("grok", GrokAdapter), ("agy", AntigravityAdapter), ("pi", PiAdapter)]

    def test_all_three_refuse_oversized_context_without_spawning(self):
        big = "x" * (LIMIT + 4096)   # one ASCII arg guaranteed over the byte cap
        for name, cls in self._adapters():
            with self.subTest(name):
                adapter = cls(session_id="judge", project_root=PLUGIN)
                # Pretend the binary is installed so the guard is reached; if the
                # guard fails to fire, sandbox.run would try to spawn — a real
                # spawn attempt (or E2BIG) here would fail the test loudly.
                with mock.patch("shutil.which", return_value="/usr/bin/" + name):
                    out = adapter.run_headless_judge(
                        prompt="REVIEW", model=None, system_context=big,
                        web_search=False, timeout_secs=5, budget_usd="2")
                self.assertIn("byte", out.lower(),
                              f"{name} did not refuse on bytes: {out[:120]!r}")
                self.assertNotIn("traceback", out.lower())


class StdinAdaptersUnaffected(unittest.TestCase):
    """claude and codex put context on STDIN, so they must NOT wire the argv
    byte guard — asserting by source so the scope can't silently widen."""

    def test_claude_and_codex_do_not_import_the_guard(self):
        for name in ("claude", "codex"):
            src = (PLUGIN / "provider" / "adapters" / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("argv_byte_error", src, f"{name} should stay stdin-only")

    def test_argv_adapters_do_wire_the_guard(self):
        for name in ("grok", "antigravity", "pi"):
            src = (PLUGIN / "provider" / "adapters" / f"{name}.py").read_text(encoding="utf-8")
            self.assertIn("argv_byte_error", src, f"{name} must carry the byte guard")


if __name__ == "__main__":
    unittest.main()
