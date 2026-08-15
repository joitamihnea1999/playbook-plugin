#!/usr/bin/env python3
"""C3 (verification-report-1.5.9): the mind-map integrity verifier must NOT
fail open on an unbalanced code fence or a stray-backtick span.

`_strip_code`/`_node_start_lines` toggle an in-fence flag on every ``` line, so
an UNBALANCED fence makes everything after it invisible to both ref resolution
and node detection — and the verifier reported CLEAN. A second vector:
`re.sub(r'`[^`]*`','',text)` ran on newline-joined text, so two stray backticks
anywhere swallowed every `[N]` between them. A merge that leaves an unbalanced
fence (conflict/truncation — exactly what merges corrupt) got a false-green on
the structural sign-off, and `--push` proceeded. A verifier that fails open is
the worst class per the inspection charter.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RI_PATH = _HERE.parent / "plugins/playbook/skills/merge/ref-integrity.py"
assert _RI_PATH.exists(), f"ref-integrity.py not found at {_RI_PATH}"
_spec = importlib.util.spec_from_file_location("ref_integrity", _RI_PATH)
ri = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ri)


class FenceFailClosed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _check(self, main_text, overflow_text=None):
        d = Path(self._tmp.name)
        main = d / "MIND_MAP.md"
        main.write_text(main_text, encoding="utf-8")
        overflow = d / "MIND_MAP_OVERFLOW.md"
        if overflow_text is not None:
            overflow.write_text(overflow_text, encoding="utf-8")
        return ri.check(main, overflow, None, None)

    # --- vector 1: unbalanced fence -----------------------------------------
    def test_unbalanced_fence_fails_closed(self):
        # A broken [99] ref hidden after an UNTERMINATED fence.
        main = ("# MIND_MAP\n\n"
                "[1] **root** — see [1]\n\n"
                "```\nnever closed — broken ref [99] hides in here\n")
        findings, _, _ = self._check(main)
        self.assertTrue(findings,
                        "unterminated fence reported CLEAN — verifier is blind past it")

    def test_same_content_unfenced_already_fails(self):
        # Control: the SAME broken ref, unfenced, is caught today — proving the
        # fence is what hid it.
        main = ("# MIND_MAP\n\n"
                "[1] **root** — see [1]\n\n"
                "prose — broken ref [99] in the open\n")
        findings, _, _ = self._check(main)
        self.assertTrue(any("[99]" in f or "99" in f for f in findings),
                        f"unfenced broken ref not caught: {findings}")

    def test_node_hidden_after_unclosed_fence(self):
        # A node DEFINED after an unclosed fence is invisible, so contiguity
        # can't notice the gap — fail closed on the fence itself.
        main = ("# MIND_MAP\n\n"
                "[1] **root** — see [1]\n"
                "[2] **mid** — see [1]\n\n"
                "```\n[3] **hidden by the open fence** — see [1]\n")
        findings, _, _ = self._check(main)
        self.assertTrue(findings, "hidden node behind open fence → false clean")

    def test_balanced_fence_still_clean(self):
        # Negative control: a properly CLOSED fence with a fake [99] example
        # inside is still correctly ignored — no false finding.
        main = ("# MIND_MAP\n\n"
                "[1] **root** — see [1]\n\n"
                "```\ncode example with [99] — illustrative only\n```\n")
        findings, _, _ = self._check(main)
        self.assertEqual(findings, [], f"balanced fence wrongly flagged: {findings}")

    # --- vector 2: stray backticks across newlines --------------------------
    def test_backticks_do_not_swallow_refs_across_newlines(self):
        # Two stray single backticks on different lines; a broken [99] between
        # them must NOT be swallowed by the inline-code strip.
        main = ("# MIND_MAP\n\n"
                "[1] **root** — see [1]\n"
                "prose with a stray ` backtick here\n"
                "broken ref [99] between the backticks\n"
                "another stray ` backtick here\n")
        findings, _, _ = self._check(main)
        self.assertTrue(any("99" in f for f in findings),
                        f"[99] swallowed by cross-newline backtick span: {findings}")

    def test_real_inline_code_still_stripped(self):
        # Negative control: a real single-line inline `list[99]` must still be
        # stripped so it doesn't false-positive.
        main = ("# MIND_MAP\n\n"
                "[1] **root** — a code span `list[99]` is not a ref, see [1]\n")
        findings, _, _ = self._check(main)
        self.assertEqual(findings, [], f"inline code wrongly read as ref: {findings}")


if __name__ == "__main__":
    unittest.main()
