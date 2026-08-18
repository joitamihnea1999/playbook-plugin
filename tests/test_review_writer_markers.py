"""Writer-side literal pins for the review/panel writeback (§2.3 + P6).

The 1.5.9 mutation scoreboard's ONE survivor (P6) was the findings-writeback
sentinel: `test_review_writeback` builds its expected marker via the code's own
`_findings_markers()`, so a rename stays self-consistent and cross-version
idempotency (against task.md files written by a prior version) is unprotected.
These tests pin the sentinel AND the salvage log names to LITERAL strings, so a
rename is caught — the negative-control-for-writers discipline the report asks
to extend to load-bearing writers.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.review import (  # noqa: E402
    _findings_markers, _judge_log_name, _neutralise_markers,
    _write_review_findings,
)


class FindingsMarkerLiterals(unittest.TestCase):
    def test_impl_markers_are_exact_literals(self):
        # Pinned to LITERALS — a rename of the sentinel breaks cross-version
        # idempotency (old task.md files carry the old marker) and must fail.
        self.assertEqual(
            _findings_markers("impl"),
            ("<!-- playbook:impl-review-findings -->",
             "<!-- /playbook:impl-review-findings -->"))

    def test_plan_markers_are_exact_literals(self):
        self.assertEqual(
            _findings_markers("plan"),
            ("<!-- playbook:plan-review-findings -->",
             "<!-- /playbook:plan-review-findings -->"))


class JudgeLogNameLiterals(unittest.TestCase):
    def test_log_names_are_exact_literals(self):
        # The hard-timeout salvage path writes `<stem>.partial.log` beside the
        # review, so these names are a cross-path contract — pin them.
        self.assertEqual(_judge_log_name("claude"), "judge.log")
        self.assertEqual(_judge_log_name("codex"), "judge-codex.log")
        self.assertEqual(_judge_log_name("antigravity"), "judge-agy.log")
        self.assertEqual(_judge_log_name("grok"), "judge-grok.log")
        self.assertEqual(_judge_log_name("pi"), "judge-pi.log")
        self.assertEqual(_judge_log_name("unknown-backend"), "judge.log")


class NeutraliseUntrustedMarkers(unittest.TestCase):
    def test_untrusted_findings_cannot_smuggle_a_delimiter(self):
        open_m, close_m = _findings_markers("impl")
        hostile = f"real finding {open_m} eat gates {close_m}"
        out = _neutralise_markers(hostile, "impl")
        # The genuine delimiters must NOT survive verbatim in the body.
        self.assertNotIn(open_m, out)
        self.assertNotIn(close_m, out)
        # But the human text is preserved (defanged, not deleted).
        self.assertIn("real finding", out)
        self.assertIn("eat gates", out)

    def test_write_then_rewrite_is_idempotent_and_bounded(self):
        # A hostile finding written once, then re-reviewed, must still leave
        # exactly one well-formed marker pair (no gate-eating drift).
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        tf = d / "task.md"
        tf.write_text(
            "# 001 - x\n## Implementation Review\n"
            "(implementation review findings appear here)\n"
            "## Work Plan\n- [ ] g\n", encoding="utf-8")
        open_m, close_m = _findings_markers("impl")
        self.assertIsNone(_write_review_findings(tf, "impl", f"A {open_m}{close_m}"))
        self.assertIsNone(_write_review_findings(tf, "impl", "B second review"))
        text = tf.read_text(encoding="utf-8")
        self.assertEqual(text.count(open_m), 1, "marker pair drifted on rerun")
        self.assertEqual(text.count(close_m), 1)
        self.assertIn("B second review", text)
        # The work plan gate survived the writes.
        self.assertIn("- [ ] g", text)


if __name__ == "__main__":
    unittest.main()
