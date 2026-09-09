#!/usr/bin/env python3
"""Cross-consumer gate-parser parity (upstream issue #09; fence-aware since task 055).

Three pieces of code decide what an unchecked gate is, and they used to disagree:
the `tasks list` progress count used a SUBSTRING (`content.count("- [ ]")`), while
head-position and the Stop hook were line-anchored. So a `- [ ]` in mid-line PROSE
was counted by the column the user sees and invisible to the gate that enforces —
a task closed at 71/74 while `status` said "(all gates checked)".

Task 055 added the second axis: a `- [ ]` quoted inside a CLOSED code fence is an
example, not a gate. Before, all three counted it "wrong-together" — and the stop
hook's `grep -m1` could pick a fenced `- [ ] Freehand…` example as FIRST_GATE and
RELEASE the stop with real gates open. Now the hook no longer greps: it asks
`scripts/task-status.py --fields`, which returns `core._live_gate_state` — the same
function `_gate_counts`/`_extract_head_position` use — so hook and CLI cannot
disagree by construction. The grep survives only as the hook's NO-PYTHON fallback,
and its one guaranteed property is pinned here too: fence-blind can only OVER-count
(a fence hides lines, never adds them), so the fallback fails CLOSED.

The invariant is a PROPERTY over a fixture table: for every case,
    progress_says_open == head_is_a_gate == hook_reader_count > 0 == expect_open
    and grep_fallback_count >= hook_reader_count
so a fourth parser cannot silently diverge again.

Pure stdlib unittest. Run: python3 tests/test_gate_parser_parity.py
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))
from tasks.core import (  # noqa: E402
    _extract_head_position, _extract_progress, _freehand_release_allowed, _gate_counts,
    _live_gate_state, _physical_lines,
)

TASK_STATUS = PLUGIN / "scripts" / "task-status.py"
STOP_HOOK_GREP = r'^[[:space:]]*- \[ \]'   # the hook's NO-PYTHON fallback, copied verbatim


def hook_fields(path: Path) -> "tuple[str, int, str, str]":
    """What the enforcing hooks read: `task-status.py --fields` → (status, live
    unchecked count, first live unchecked gate text, freehand `release`/`hold`).
    Exactly the bytes the hook consumes — LF-only, four lines."""
    r = subprocess.run([sys.executable, str(TASK_STATUS), "--fields", str(path)],
                       capture_output=True)
    assert r.returncode == 0, r.stderr
    out = r.stdout.decode("utf-8")
    assert b"\r" not in r.stdout, "hook fields must be LF-only bytes"
    parts = out.split("\n")
    assert len(parts) == 5 and parts[4] == "", f"frame must be exactly 4 LF-terminated lines: {out!r}"
    status, count, first, release = parts[:4]
    assert release in ("release", "hold"), release
    return status, int(count), first, release


def hook_unchecked(path: Path) -> int:
    """The hook's own count (Python-backed)."""
    return hook_fields(path)[1]


def grep_fallback_unchecked(path: Path) -> int:
    """The Stop hook's no-python fallback: grep -cE '^[[:space:]]*- \\[ \\]'."""
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
    ("CRLF all checked", "- [x] a\r\n- [x] b\r\n", False),
    ("unicode gate text, one open", "- [x] éöк done\n- [ ] задача: run 测试\n", True),
    ("unicode gate text, all checked", "- [x] 完了 ✓\n", False),
    ("tab-indented open gate", "- [x] a\n\t- [ ] tabbed\n", True),
    # Task 055: a fenced line-start marker is an EXAMPLE for all three (was
    # "wrong-together" — counted by all, and a Freehand decoy could release the
    # stop). Fence rules are the shared engine's (core._iter_fenced_flags).
    ("fenced line-start example after all-checked", "## Work\n- [x] real\n```\n- [ ] <describe the gate>\n```\n", False),
    ("fenced Freehand decoy BEFORE a real open gate (the 043 round-5 vector)",
     "## Work\n```\n- [ ] Freehand — example only\n```\n- [x] a\n- [ ] real work left\n", True),
    ("tilde-fenced decoy", "~~~md\n- [ ] example\n~~~\n- [x] a\n", False),
    ("longer closer still closes", "````\n- [ ] example\n`````\n- [x] a\n", False),
    ("shorter closer does NOT close — but an UNCLOSED fence hides nothing (fail closed)",
     "````\n- [ ] still live\n```\n- [x] a\n", True),
    ("unclosed trailing fence: the real gate stays LIVE",
     "- [x] a\n```\n- [ ] real gate inside a fence the author forgot to close\n", True),
    ("indented (4-space) fence opener is NOT a fence — its example is live",
     "- [x] a\n    ```\n    - [ ] counted\n    ```\n", True),
    ("nested gate after a blank line is a GATE, not indented code",
     "- [x] a\n\n    - [ ] nested real gate\n", True),
    ("inline-code opener (backtick in info string) is not a fence",
     "``` `x` ```\n- [ ] live\n", True),
    ("CRLF fenced example, all checked", "- [x] a\r\n```\r\n- [ ] ex\r\n```\r\n", False),
    ("NBSP after the closer does not close (V6) → unclosed → live",
     "```\n- [ ] live\n```\u00a0\n- [x] a\n", True),
    # Round-1 panel (task 055)
    ("blank-text first gate followed by a Freehand gate: the blank gate is FIRST",
     "- [ ]\n- [ ] Freehand — work is done\n", True),
    ("NBSP-leading marker is not a gate for ANY reader (sonnet/grok round 1)",
     "- [x] a\n\u00a0- [ ] not a gate\n", False),
    ("empty required field before a gate: head stops at the field, the gate is still the first gate",
     "- **Owner**:\n- [ ] g\n", True),
    ("U+0085 is NOT a line boundary — physical lines only, like grep (codex round 1)",
     "prose\u0085- [ ] x\n- [x] a\n", False),
    ("U+2028 / VT / FF are NOT line boundaries either",
     "p\u2028- [ ] x\x0b- [ ] y\x0c- [ ] z\n- [x] a\n", False),
    # Round-2 panel (opus): all gates checked but an empty required field remains —
    # no GATE is open (all readers agree), the head reports the field (documented extra).
    ("all checked + empty required field: no open gate, head shows the field",
     "- **Owner**:\n- [x] a\n", False),
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
                head = _extract_head_position(tf)
                # head_open = "the head is an open GATE". An empty required field
                # (`- **Field**:`) is the head's one documented extra — it is not
                # a gate, so it does not count as open here; pinned below.
                head_is_field = head.startswith("- **")
                head_open = not head.startswith("(") and not head_is_field
                if head_is_field:
                    self.assertTrue(head.endswith(":"), f"head field shape: {label}")
                _status, hook_count, hook_first, _release = hook_fields(tf)
                hook_open = hook_count > 0
                cli_unchecked, cli_total, cli_first = _live_gate_state(_physical_lines(content))

                self.assertEqual(progress_open, expect_open, f"progress: {label}")
                self.assertEqual(hook_open, expect_open, f"stop-hook: {label}")
                if head_is_field:
                    # The head stopped EARLY at a required field: it says nothing
                    # about gates; the count and the hook still carry the truth.
                    self.assertEqual(progress_open, hook_open, f"count vs hook: {label}")
                else:
                    self.assertEqual(head_open, expect_open, f"head: {label}")
                    # The property itself: all three identical.
                    self.assertEqual({progress_open, head_open, hook_open}, {expect_open},
                                     f"consumers disagree: {label}")
                # The hook's count and FIRST_GATE ARE the CLI's (`_live_gate_state`,
                # same function, same fixture). `tasks status`'s head position takes
                # its gate from the same scan but may stop EARLIER at an empty
                # required field (`- **Field**:`) — the one documented difference.
                self.assertEqual(hook_count, total - checked, f"hook count != CLI count: {label}")
                self.assertEqual((hook_count, hook_first), (cli_unchecked, cli_first), f"hook != CLI: {label}")
                if expect_open and not head_is_field:
                    self.assertEqual(head, hook_first, f"head != hook first gate: {label}")
                if not expect_open:
                    self.assertEqual(hook_first, "", f"no open gate → empty first: {label}")
                # The no-python fallback may only OVER-count (fail closed).
                self.assertGreaterEqual(grep_fallback_unchecked(tf), hook_count,
                                        f"grep fallback under-counted: {label}")

    # (content, expect_release) — the ONE Freehand-release rule, decided in Python
    # (`_freehand_release_allowed`) and shipped to the hook as the 4th field. The
    # round-1 panel showed the hook's own `Freehand*` prefix test on the first-gate
    # text released on decoys the COUNT correctly kept live.
    RELEASE_CASES = [
        ("real column-0 Freehand debrief gate", "## Debrief\n- [ ] Freehand — work is done\n", True),
        ("bare Freehand", "- [ ] Freehand\n", True),
        ("Freehand log is the cleanup gate — never released", "- [ ] Freehand log\n", False),
        ("Freehand log with punctuation", "- [ ] Freehand log — flush\n", False),
        ("Freehand logging (word continuation) IS released, like the hook's old glob",
         "- [ ] Freehand logging\n", True),
        ("no open gates → nothing to release", "- [x] Freehand\n", False),
        ("blank first gate, Freehand second (sentinel bug)", "- [ ]\n- [ ] Freehand\n", False),
        ("real gate first", "- [ ] work\n- [ ] Freehand\n", False),
        ("closed-fenced Freehand decoy before a real gate", "```\n- [ ] Freehand\n```\n- [ ] work\n", False),
        ("UNCLOSED-fenced Freehand decoy: counted (fail closed) but never released",
         "```\n- [ ] Freehand\n- [ ] work\n", False),
        ("unclosed fence ANYWHERE holds the release even for a real Freehand first gate",
         "- [ ] Freehand\n```\nexample\n", False),
        ("indented (4-space) Freehand example is counted but never released (grok round 1)",
         "    - [ ] Freehand example\n- [ ] work\n", False),
        ("tab-indented Freehand example: 4 columns → hold", "\t- [ ] Freehand\n", False),
        ("1-3 space indent is still a live template gate", "   - [ ] Freehand\n", True),
        ("CRLF real Freehand", "- [ ] Freehand — done\r\n", True),
    ]

    def test_freehand_release_rule_is_the_hooks_fourth_field(self):
        for label, content, expect_release in self.RELEASE_CASES:
            with self.subTest(label):
                d = Path(tempfile.mkdtemp())
                tf = d / "task.md"
                tf.write_bytes(content.encode("utf-8"))
                py = _freehand_release_allowed(_physical_lines(content))
                self.assertEqual(py, expect_release, f"python rule: {label}")
                _s, _c, _f, release = hook_fields(tf)
                self.assertEqual(release == "release", expect_release, f"hook field: {label}")

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
