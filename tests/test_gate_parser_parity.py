#!/usr/bin/env python3
"""Cross-consumer gate-parser parity (upstream issue #09).

Three pieces of code decide what an unchecked gate is, and they used to disagree:
the `tasks list` progress count used a SUBSTRING (`content.count("- [ ]")`), while
head-position and the Stop hook were line-anchored. So a `- [ ]` in mid-line PROSE
was counted by the column the user sees and invisible to the gate that enforces —
a task closed at 71/74 while `status` said "(all gates checked)".

Testing the three one at a time is what let them drift. The invariant here is a
PROPERTY over a fixture table: for every case,
    progress_says_open  ==  head_is_a_gate  ==  stop_hook_grep_count > 0
so a fourth parser cannot silently diverge again. The Stop hook's rule is exercised
as the literal grep it runs (`^[[:space:]]*- \[ \]`), not a paraphrase.

Pure stdlib unittest. Run: python3 tests/test_gate_parser_parity.py
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.core import _extract_head_position, _extract_progress, _gate_counts  # noqa: E402

STOP_HOOK_GREP = r'^[[:space:]]*- \[ \]'   # copied verbatim from scripts/stop-hook


def hook_unchecked(path: Path) -> int:
    """The Stop hook's own count: grep -cE '^[[:space:]]*- \\[ \\]'."""
    r = subprocess.run(["grep", "-cE", STOP_HOOK_GREP, str(path)],
                       capture_output=True, text=True)
    # grep -c prints the count and exits 1 when zero matches.
    return int((r.stdout or "0").strip() or "0")


CHECKED_71 = "".join(f"- [x] G{i}: done\n" for i in range(71))

# (label, content, expect_open) — expect_open is the single truth all three must share.
CASES = [
    ("71 checked + 3 prose mentions (the report)",
     "## Status\npending\n" + CHECKED_71
     + "Template shows `- [ ]` for an open gate.\n"
       "A reviewer asked why `- [ ]` was still in the draft.\n"
       "The convention is `- [ ]` until the gate's work lands.\n",
     False),
    ("one real open gate", "- [x] a\n- [ ] b\n", True),
    ("all checked, nothing else", "- [x] a\n- [x] b\n", False),
    ("indented real gate", "- [x] a\n    - [ ] nested\n", True),
    ("capitalised checked mark only", "- [X] done\n", False),
    ("CRLF open gate", "- [x] a\r\n- [ ] b\r\n", True),
    # Documented residual: a fenced line-start marker is counted by ALL three —
    # consistently wrong, not silently divergent. Parity still holds (all True).
    ("fenced line-start example (wrong-together, but agreeing)",
     "## Work\n- [x] real\n```\n- [ ] <describe the gate>\n```\n", True),
]


class GateParserParity(unittest.TestCase):
    def test_all_three_agree_on_every_case(self):
        for label, content, expect_open in CASES:
            with self.subTest(label):
                d = Path(tempfile.mkdtemp())
                tf = d / "task.md"
                tf.write_bytes(content.encode("utf-8"))  # preserve CRLF exactly

                checked, total = _gate_counts(content)
                progress_open = total > checked
                head_open = not _extract_head_position(tf).startswith("(")
                hook_open = hook_unchecked(tf) > 0

                self.assertEqual(progress_open, expect_open, f"progress: {label}")
                self.assertEqual(head_open, expect_open, f"head: {label}")
                self.assertEqual(hook_open, expect_open, f"stop-hook: {label}")
                # The property itself: all three identical.
                self.assertEqual({progress_open, head_open, hook_open}, {expect_open},
                                 f"consumers disagree: {label}")

    def test_reported_file_reports_71_71_not_71_74(self):
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text(
            "## Status\npending\n" + CHECKED_71
            + "The convention is `- [ ]` until the gate's work lands.\n",
            encoding="utf-8")
        self.assertEqual(_extract_progress(tf), "71/71")


if __name__ == "__main__":
    unittest.main()
