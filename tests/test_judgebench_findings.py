#!/usr/bin/env python3
"""Step 4 tests: findings parser (`bench/lib/scoring.py` part 1).

The parser reads the TRAILING `FINDINGS:` … `END FINDINGS` block the template
requires. It must never raise: a judge that cannot follow the format is a
result (`malformed`), not an error. Adversarial inputs included.
"""
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.lib import scoring  # noqa: E402

WELL_FORMED = """Some free-text review first. I looked at the diff and the callers.

FINDINGS:
1. FILE: plugins/playbook/tasks/core.py
   SYMBOL: extract_risk
   SEVERITY: Critical
   WHY: A fenced `## Risk` heading shadows the live one, so the close reads the
   wrong class (core.py:1004). Scenario: a code block containing "## Risk\\nreversible".
2. FILE: `tests/test_core.py:88`
   SYMBOL: -
   SEVERITY: Minor
   WHY: The test asserts the mock, not the output.
END FINDINGS
"""


class WellFormedTests(unittest.TestCase):
    def test_parses_entries(self):
        r = scoring.parse_findings(WELL_FORMED)
        self.assertEqual(r.status, "ok")
        self.assertEqual(len(r.findings), 2)
        f1, f2 = r.findings
        self.assertEqual((f1.n, f1.file, f1.symbol, f1.claimed_severity),
                         (1, "plugins/playbook/tasks/core.py", "extract_risk", "Critical"))
        self.assertIn("shadows the live one", f1.text)
        self.assertIn("wrong class", f1.text)          # multi-line WHY joined
        self.assertEqual((f2.file, f2.symbol, f2.claimed_severity, f2.line),
                         ("tests/test_core.py", None, "Minor", 88))   # backticks + :line stripped
        self.assertEqual(r.errors, [])

    def test_case_insensitive_keys_and_loose_whitespace(self):
        text = "findings:\n1.  file:  a/b.py \n severity: important\nWhy: x\nend findings\n"
        r = scoring.parse_findings(text)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.findings[0].file, "a/b.py")
        self.assertEqual(r.findings[0].claimed_severity, "Important")

    def test_last_block_wins(self):
        text = ("FINDINGS:\n1. FILE: old.py\n   SEVERITY: Minor\n   WHY: draft\nEND FINDINGS\n"
                "Actually, revised:\n"
                "FINDINGS:\n1. FILE: new.py\n   SEVERITY: Critical\n   WHY: final\nEND FINDINGS\n")
        r = scoring.parse_findings(text)
        self.assertEqual([f.file for f in r.findings], ["new.py"])

    def test_leading_prose_and_trailing_cap_line_ignored(self):
        text = WELL_FORMED + "\nCAP: 2/5 reported, exhausted\n"
        r = scoring.parse_findings(text)
        self.assertEqual(len(r.findings), 2)


class EmptyAndMalformedTests(unittest.TestCase):
    def test_none_sentinel_is_empty_ok(self):
        r = scoring.parse_findings("I found nothing.\n\nFINDINGS:\nNONE\nEND FINDINGS\n")
        self.assertEqual((r.status, r.findings), ("empty", []))

    def test_no_block_is_malformed(self):
        r = scoring.parse_findings("Here are my thoughts: the code is fine, ship it.")
        self.assertEqual(r.status, "malformed")
        self.assertEqual(r.findings, [])
        self.assertTrue(any("FINDINGS" in e for e in r.errors))

    def test_block_without_entries_is_malformed(self):
        r = scoring.parse_findings("FINDINGS:\nsome prose but no FILE lines\nEND FINDINGS\n")
        self.assertEqual(r.status, "malformed")

    def test_empty_and_none_inputs(self):
        for text in ("", None, "   \n"):
            r = scoring.parse_findings(text)
            self.assertEqual(r.status, "malformed")

    def test_adapter_error_envelopes_are_malformed_not_crash(self):
        for text in ("(error: codex not found on PATH)", "(FAILED — exit 1)\n[stderr tail]\nboom"):
            r = scoring.parse_findings(text)
            self.assertEqual(r.status, "malformed")

    def test_unterminated_block_parses_to_eof_with_error(self):
        r = scoring.parse_findings("FINDINGS:\n1. FILE: a.py\n   SEVERITY: Minor\n   WHY: y\n")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.findings[0].file, "a.py")
        self.assertTrue(any("END FINDINGS" in e for e in r.errors))

    def test_entry_without_file_is_skipped_with_error(self):
        text = "FINDINGS:\n1. SYMBOL: f\n   SEVERITY: Minor\n   WHY: no file\n2. FILE: b.py\n   SEVERITY: Minor\n   WHY: ok\nEND FINDINGS\n"
        r = scoring.parse_findings(text)
        self.assertEqual([f.file for f in r.findings], ["b.py"])
        self.assertTrue(any("FILE" in e for e in r.errors))

    def test_unknown_severity_recorded_verbatim_and_flagged(self):
        text = "FINDINGS:\n1. FILE: a.py\n   SEVERITY: Blocker\n   WHY: y\nEND FINDINGS\n"
        r = scoring.parse_findings(text)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.findings[0].claimed_severity, "Blocker")
        self.assertFalse(r.findings[0].severity_known)
        self.assertTrue(any("severity" in e.lower() for e in r.errors))

    def test_missing_severity(self):
        r = scoring.parse_findings("FINDINGS:\n1. FILE: a.py\n   WHY: y\nEND FINDINGS\n")
        self.assertEqual(r.findings[0].claimed_severity, "")
        self.assertFalse(r.findings[0].severity_known)


class AdversarialTests(unittest.TestCase):
    def test_keys_inside_code_fences_are_not_entries(self):
        text = ("FINDINGS:\n1. FILE: real.py\n   SEVERITY: Minor\n   WHY: the template says\n"
                "   ```\n   2. FILE: fake.py\n   SEVERITY: Critical\n   ```\n   and that is all\n"
                "END FINDINGS\n")
        r = scoring.parse_findings(text)
        self.assertEqual([f.file for f in r.findings], ["real.py"])

    def test_many_duplicate_blocks_collapse(self):
        entry = "{n}. FILE: a.py\n   SYMBOL: f\n   SEVERITY: Minor\n   WHY: same\n"
        text = "FINDINGS:\n" + "".join(entry.format(n=i) for i in range(1, 201)) + "END FINDINGS\n"
        r = scoring.parse_findings(text)
        self.assertEqual(r.status, "ok")
        self.assertEqual(len(r.findings), 1)
        self.assertTrue(any("duplicate" in e for e in r.errors))

    def test_unicode_and_crlf(self):
        text = "FINDINGS:\r\n1. FILE: naïve/päth.py\r\n   SYMBOL: ƒ\r\n   SEVERITY: Important\r\n   WHY: ünïcödé → ok\r\nEND FINDINGS\r\n"
        r = scoring.parse_findings(text)
        self.assertEqual(r.findings[0].file, "naïve/päth.py")
        self.assertEqual(r.findings[0].symbol, "ƒ")
        self.assertIn("→", r.findings[0].text)

    def test_huge_input_does_not_raise(self):
        r = scoring.parse_findings("x" * 2_000_000)
        self.assertEqual(r.status, "malformed")

    def test_path_normalization(self):
        for raw, want_file, want_line in (
                ("`a/b.py`", "a/b.py", None), ("'a/b.py'", "a/b.py", None),
                ("./a/b.py:12", "a/b.py", 12), ("a/b.py:12:5", "a/b.py", 12),
                ("a\\b.py", "a/b.py", None), ("a/b.py (line 7)", "a/b.py", 7)):
            with self.subTest(raw=raw):
                f, ln = scoring.normalize_file_ref(raw)
                self.assertEqual((f, ln), (want_file, want_line))

    def test_to_dict_roundtrip(self):
        r = scoring.parse_findings(WELL_FORMED)
        d = r.findings[0].to_dict()
        self.assertEqual(set(d), {"n", "file", "symbol", "line", "claimed_severity",
                                  "severity_known", "text"})
        self.assertEqual(scoring.Finding.from_dict(d), r.findings[0])


if __name__ == "__main__":
    unittest.main()
