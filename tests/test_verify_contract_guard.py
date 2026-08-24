#!/usr/bin/env python3
"""T5 — guard the verify contract itself.

`.agent/config.json` is gate-exempt, so an agent can silently weaken/delete the
`verify` command and then close against a hollow verify. Two guards make that
VISIBLE: (a) the close path journals the resolved verify command(s); (b)
`tasks audit` flags a verify command that changed since the most recent prior
close receipt. This module pins both, each with a negative control.

Run: python3 tests/test_verify_contract_guard.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

from tasks.audit import check_verify_contract_change, run_audit  # noqa: E402
from tasks.core import format_verify_receipt, upsert_task_section  # noqa: E402


class VerifyContractSweep(unittest.TestCase):
    def _project(self, verify) -> Path:
        d = Path(tempfile.mkdtemp(prefix="pb-vcg-"))
        (d / ".agent" / "tasks").mkdir(parents=True)
        cfg = {} if verify is None else {"verify": verify}
        (d / ".agent" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return d

    def _add_receipt(self, d: Path, num: str, ts: str, risk: str,
                     commands: "list[str]", *, prose_mention: bool = False) -> None:
        """Write a task whose receipt is built by the REAL format_verify_receipt
        (so the sweep parses production shapes), optionally with a prose mention
        of the heading in References (which must NOT be read as the section)."""
        td = d / ".agent" / "tasks" / f"{num}-t"
        td.mkdir(parents=True)
        body = f"# {num} - T\n\n## Status\ndone\n"
        if prose_mention:
            body += ("\n## References\n- see the `## Verification Receipt` "
                     "section below for the recorded commands\n")
        body += "\n## Verification Receipt\n\n(receipt appears here)\n"
        tf = td / "task.md"
        tf.write_text(body, encoding="utf-8")
        entries = [("verify", c, 0, "ok") for c in commands]
        receipt = format_verify_receipt(entries, "abc1234", risk, timestamp=ts)
        upsert_task_section(tf, "Verification Receipt", receipt)

    def test_no_baseline_is_clean(self):
        # First close ever — no prior receipt. "Nothing to flag" is None (silent).
        self.assertIsNone(check_verify_contract_change(self._project("python3 scripts/verify")))

    def test_unchanged_verify_is_clean(self):
        d = self._project("python3 scripts/verify")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])
        self.assertIsNone(check_verify_contract_change(d))

    def test_weakened_verify_is_flagged(self):
        # Negative control: a receipt recorded a real command; config now runs
        # `true` — the dropped command MUST be flagged (else it is a false-green).
        d = self._project("true")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])
        r = check_verify_contract_change(d)
        self.assertEqual(r["status"], "findings")
        self.assertEqual(r["severity"], "advisory")      # visible, not a hard block
        self.assertIn("python3 scripts/verify", r["output"])   # names the dropped cmd

    def test_deleted_verify_is_flagged(self):
        d = self._project(None)                          # no `verify` key at all
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])
        self.assertEqual(check_verify_contract_change(d)["status"], "findings")

    def test_strengthening_is_not_flagged(self):
        # Adding a command (a stronger bar) drops nothing → clean.
        d = self._project({"_always": ["python3 scripts/verify", "python3 -m ruff"]})
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])
        self.assertIsNone(check_verify_contract_change(d))

    def test_laundering_does_not_hide_a_weakening(self):
        # opus panel finding: weaken → close (records the weak cmd as a NEW
        # receipt) → the union still contains the strong cmd from the earlier
        # close, so the drop stays flagged forever. Most-recent-only would go
        # clean here; the union must not.
        d = self._project("true")                        # config already weakened
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])    # the strong, earlier close
        self._add_receipt(d, "002", "2026-06-01T00:00:00+00:00", "reversible",
                          ["true"])                       # the laundering close
        r = check_verify_contract_change(d)
        self.assertEqual(r["status"], "findings",
                         "a weaken-then-close laundered the baseline clean")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_prose_mention_is_not_read_as_a_receipt(self):
        # grok panel finding (round-1 + round-3): the REAL regression is a task
        # whose References MENTION `## Verification Receipt` in the SAME FILE as
        # its real receipt. A substring `text.find("## Verification Receipt")`
        # would land on the prose line (above the receipt) and slice an empty
        # region — reading the task as baseline-less and MISSING the weakening.
        # This is the ONLY task, so the flag can only come from correctly parsing
        # the real heading past the prose mention (exact-line match).
        d = self._project("true")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"], prose_mention=True)
        r = check_verify_contract_change(d)
        self.assertIsNotNone(
            r, "a prose mention above the real receipt masked the weakening")
        self.assertEqual(r["status"], "findings")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_risk_keyed_full_surface_no_false_positive(self):
        # An assertive close recorded base + claim; the current risk-keyed config
        # still declares both (across risks) → nothing dropped → clean.
        d = self._project({"_always": ["base"], "assertive": ["claim-check"]})
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "assertive",
                          ["base", "claim-check"])
        self.assertIsNone(check_verify_contract_change(d))

    def test_cross_risk_key_move_is_flagged(self):
        # round-3 panel (grok/opus): moving a command from `_always` to a single
        # risk key DROPS it from every OTHER risk's bar. An all-risk-union read
        # of "current" sees the command under `irreversible` and calls it still
        # present → false clean, while every reversible/assertive close silently
        # stops running it. The sweep must compare each receipt's commands
        # against the current contract FOR THAT RECEIPT'S OWN RISK.
        d = self._project({"irreversible": ["python3 scripts/verify"]})
        # a past REVERSIBLE close ran the command back when it was in `_always`
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])
        r = check_verify_contract_change(d)
        self.assertIsNotNone(
            r, "a command moved to a narrower risk key was not flagged")
        self.assertEqual(r["status"], "findings")
        self.assertEqual(r["severity"], "advisory")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_cross_risk_key_move_clean_when_receipt_risk_still_covers(self):
        # Control for the risk-keyed comparison: the SAME move is clean for a
        # risk whose bar still includes the command. An `irreversible` close's
        # bar still runs it → nothing dropped for that risk.
        d = self._project({"irreversible": ["python3 scripts/verify"]})
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "irreversible",
                          ["python3 scripts/verify"])
        self.assertIsNone(check_verify_contract_change(d))

    def test_acknowledged_removal_still_emits_informational_line(self):
        # OWNER DECISION (round 3): the ack list lives on the SAME gate-exempt
        # `.agent/config.json` path it guards, so an agent that drops `verify`
        # can add the dropped command to `verify_contract_ack` in the same write.
        # Prevention is therefore impossible; VISIBILITY is the guarantee. An
        # acknowledged removal is never FULLY silenced — it still emits a
        # low-severity (info, below advisory) line naming the removed command and
        # its ack, and never fails the audit.
        d = Path(tempfile.mkdtemp(prefix="pb-vcg-"))
        (d / ".agent" / "tasks").mkdir(parents=True)
        (d / ".agent" / "config.json").write_text(
            json.dumps({"verify": "pnpm test",
                        "verify_contract_ack": ["npm test"]}), encoding="utf-8")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["npm test"])           # old name, now dropped + ack'd
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "an ack'd removal was fully silenced")
        self.assertEqual(r["status"], "findings")
        self.assertEqual(r["severity"], "info")   # below advisory — never fails
        self.assertIn("npm test", r["output"])    # names the acknowledged drop
        self.assertTrue(run_audit(d)["passed"],   # info tier never fails audit
                        "an informational ack line must not fail the audit")

    def test_multiline_verify_reported_as_unsupported_for_drift(self):
        # B5: a MULTI-LINE verify command is compared only by its first line
        # (cmd1) against the receipt (which also records only cmd1), so a
        # weakening confined to lines 2+ is INVISIBLE. Here cmd1 ("make lint") is
        # unchanged, so the old cmd1-only comparison returned None (silent
        # false-clean). The sweep must instead surface the multi-line command
        # loudly as unsupported-for-drift-detection — never silence.
        d = self._project("make lint\nmake test")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["make lint\nmake test"])
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "a multi-line verify command was silently passed")
        self.assertEqual(r["status"], "findings")
        self.assertEqual(r["severity"], "advisory")     # visible, never a hard block
        self.assertIn("multi-line", r["output"].lower())
        self.assertIn("make lint", r["output"])          # names the command (cmd1)

    def test_single_line_verify_has_no_multiline_advisory(self):
        # Control: a single-line verify with a matching receipt stays fully clean
        # — the multi-line advisory must NOT fire spuriously.
        d = self._project("make test")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["make test"])
        self.assertIsNone(check_verify_contract_change(d))

    def test_unacknowledged_removal_still_fires_with_ack_present(self):
        # ack drops the named command to informational; a DIFFERENT unack'd drop
        # still fires at ADVISORY (finding) severity — the negative control.
        d = Path(tempfile.mkdtemp(prefix="pb-vcg-"))
        (d / ".agent" / "tasks").mkdir(parents=True)
        (d / ".agent" / "config.json").write_text(
            json.dumps({"verify": "pnpm test",
                        "verify_contract_ack": ["npm test"]}), encoding="utf-8")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["npm test", "python3 scripts/verify"])
        r = check_verify_contract_change(d)
        self.assertEqual(r["status"], "findings")
        self.assertEqual(r["severity"], "advisory")    # an unack'd drop is a finding
        # per-risk render (round-8): the unack'd drop is on a `[risk] dropped:` line
        dropped_line = next(ln for ln in r["output"].splitlines()
                            if "dropped:" in ln and "python3 scripts/verify" in ln)
        self.assertIn("[reversible]", dropped_line)
        self.assertNotIn("'npm test'", dropped_line)   # ack'd one not in the dropped list
        # ...but the ack'd removal is still NAMED (in the acknowledged section)
        self.assertIn("npm test", r["output"])
        # round-5 panel (grok): the guidance must NOT promise an ack "clears" the
        # finding — an ack only downgrades a removal to an informational line.
        self.assertNotIn("to clear this", r["output"])
        self.assertIn("informational", r["output"])

    def test_cross_risk_render_is_not_self_contradictory(self):
        # round-8 panel (grok/codex): with BOTH a reversible and an irreversible
        # receipt, a command moved into `irreversible` is dropped from reversible
        # but current in irreversible. A flat render showed `dropped:[X]` and
        # `current:[X]` together — a self-contradiction an agent dismisses. The
        # per-risk render must attribute the drop to reversible only.
        d = self._project({"irreversible": ["python3 scripts/verify"]})
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])
        self._add_receipt(d, "002", "2026-02-01T00:00:00+00:00", "irreversible",
                          ["python3 scripts/verify"])
        r = check_verify_contract_change(d)
        self.assertEqual(r["status"], "findings")
        dropped_line = next(ln for ln in r["output"].splitlines()
                            if "dropped:" in ln and "python3 scripts/verify" in ln)
        self.assertIn("[reversible]", dropped_line)       # attributed to reversible
        self.assertNotIn("[irreversible] dropped", r["output"])  # NOT dropped there

    def test_baseline_spans_all_lanes(self):
        # panel: verify is a repo-global contract; a weakening recorded in
        # ANOTHER lane must baseline the current lane too.
        d = Path(tempfile.mkdtemp(prefix="pb-vcg-"))
        (d / ".agent" / "bob" / "tasks").mkdir(parents=True)   # current lane: empty
        (d / ".agent" / "config.json").write_text(
            json.dumps({"verify": "true"}), encoding="utf-8")
        (d / ".agent" / "current_user").write_text("bob\n", encoding="utf-8")
        # alice's lane recorded a strong close
        atd = d / ".agent" / "alice" / "tasks" / "001-t"
        atd.mkdir(parents=True)
        atf = atd / "task.md"
        atf.write_text("# 001 - T\n\n## Status\ndone\n\n## Verification Receipt\n\n"
                       "(receipt appears here)\n", encoding="utf-8")
        entries = [("verify", "python3 scripts/verify", 0, "ok")]
        upsert_task_section(atf, "Verification Receipt",
                            format_verify_receipt(entries, "abc1234", "reversible",
                                                  timestamp="2026-01-01T00:00:00+00:00"))
        r = check_verify_contract_change(d)
        self.assertEqual(r["status"], "findings",
                         "a weakening recorded in another lane was missed")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_mixed_timestamp_forms_do_not_crash_the_audit(self):
        # round-5 panel (codex/grok): the regex accepts both offset-aware
        # (`...+00:00`) and naive (`...`) receipt timestamps; `datetime >`
        # comparison of an aware vs a naive value raises TypeError (NOT
        # ValueError), and run_audit does not wrap this check — so a mixed pair
        # would abort the ENTIRE audit and record nothing. Detection must survive
        # a mixed pair and still flag the drop.
        d = self._project("true")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])          # offset-aware
        self._add_receipt(d, "002", "2026-02-01T00:00:00", "reversible",
                          ["python3 scripts/verify"])          # naive
        r = check_verify_contract_change(d)                    # must not raise
        self.assertEqual(r["status"], "findings")
        self.assertIn("python3 scripts/verify", r["output"])
        self.assertTrue("passed" in run_audit(d))              # whole audit ran

    def _write_raw_task(self, d: Path, num: str, body: str) -> Path:
        td = d / ".agent" / "tasks" / f"{num}-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(body, encoding="utf-8")
        return td

    def test_fenced_receipt_heading_is_ignored(self):
        # round-6 panel (codex): the section is found by an exact heading LINE,
        # but a ``` fenced EXAMPLE of `## Verification Receipt` above the real
        # section matches that line too — the parser would stop at the fake,
        # read an empty baseline, and MISS the weakening. Fence-aware scanning
        # must skip the example and read the real receipt below it.
        d = self._project("true")
        body = (
            "# 001 - T\n\n## Status\ndone\n\n## Notes\n"
            "Example of the format:\n```\n## Verification Receipt\n"
            "### 2020-01-01T00:00:00+00:00 · risk reversible · commit deadbee\n"
            "    - [PASS] `decoy-should-be-ignored` (verify)\n```\n\n"
            "## Verification Receipt\n\n"
            "### 2026-01-01T00:00:00+00:00 · risk reversible · commit abc1234\n"
            "- **Commands:**\n    - [PASS] `python3 scripts/verify` (verify)\n")
        self._write_raw_task(d, "001", body)
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "a fenced heading example masked the real receipt")
        self.assertEqual(r["status"], "findings")
        self.assertIn("python3 scripts/verify", r["output"])
        self.assertNotIn("decoy-should-be-ignored", r["output"])

    def test_mixed_fence_delimiters_do_not_hide_the_receipt(self):
        # round-7 panel (codex): a naive per-line fence toggle flips state on ANY
        # ``` or ~~~ marker. A 4-backtick fence containing a literal ~~~ line
        # would toggle OFF at the ~~~, leaving the scanner "inside" after the
        # real 4-backtick close — so the real receipt below is skipped and the
        # weakening missed. Fence tracking must key on the delimiter char+length
        # (CommonMark), like core._risk_heading_lines.
        d = self._project("true")
        body = (
            "# 001 - T\n\n## Status\ndone\n\n## Notes\n"
            "````\n## Verification Receipt\n~~~\n"
            "### 2020-01-01T00:00:00+00:00 · risk reversible · commit deadbee\n"
            "    - [PASS] `decoy-should-be-ignored` (verify)\n````\n\n"
            "## Verification Receipt\n\n"
            "### 2026-01-01T00:00:00+00:00 · risk reversible · commit abc1234\n"
            "- **Commands:**\n    - [PASS] `python3 scripts/verify` (verify)\n")
        self._write_raw_task(d, "001", body)
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "a mixed-delimiter fence masked the real receipt")
        self.assertEqual(r["status"], "findings")
        self.assertIn("python3 scripts/verify", r["output"])
        self.assertNotIn("decoy-should-be-ignored", r["output"])

    def test_unclosed_fence_does_not_hide_the_receipt(self):
        # round-8 panel (codex-sol/grok): an UNCLOSED ``` before the real receipt
        # would leave a naive scanner "inside" to EOF, skipping the receipt →
        # empty baseline → missed weakening. Fail closed: an unterminated fence
        # skips nothing, so the receipt below is still read.
        d = self._project("true")
        body = (
            "# 001 - T\n\n## Status\ndone\n\n## Notes\n"
            "```\nan unterminated fence — never closed\n\n"
            "## Verification Receipt\n\n"
            "### 2026-01-01T00:00:00+00:00 · risk reversible · commit abc1234\n"
            "- **Commands:**\n    - [PASS] `python3 scripts/verify` (verify)\n")
        self._write_raw_task(d, "001", body)
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "an unclosed fence masked the real receipt")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_indented_fence_marker_does_not_hide_the_receipt(self):
        # round-8 panel (codex-terra): a 4-space-indented ``` is an indented code
        # block, NOT a CommonMark fence — it must not open a fence and hide the
        # receipt below. Fence detection uses the <=3-space rule on the raw line.
        d = self._project("true")
        body = (
            "# 001 - T\n\n## Status\ndone\n\n## Notes\n"
            "    ```\n    looks fenced but is 4-space-indented (not a fence)\n\n"
            "## Verification Receipt\n\n"
            "### 2026-01-01T00:00:00+00:00 · risk reversible · commit abc1234\n"
            "- **Commands:**\n    - [PASS] `python3 scripts/verify` (verify)\n")
        self._write_raw_task(d, "001", body)
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "a 4-space-indented ``` masked the real receipt")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_indented_receipt_entry_is_still_read(self):
        # round-10 panel (grok): the `### … · risk …` entry was matched on the
        # RAW line (^###), while headings/bullets strip — so a 1-3-space-indented
        # entry orphaned its command bullets → empty baseline → false clean. All
        # matching is now strip-consistent.
        d = self._project("true")
        body = (
            "# 001 - T\n\n## Status\ndone\n\n## Verification Receipt\n\n"
            "  ### 2026-01-01T00:00:00+00:00 · risk reversible · commit abc1234\n"
            "  - **Commands:**\n      - [PASS] `python3 scripts/verify` (verify)\n")
        self._write_raw_task(d, "001", body)
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "an indented ### entry orphaned its command bullets")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_fence_closer_with_trailing_content_does_not_close(self):
        # round-10 panel (codex): a CommonMark closer is a run of the delimiter
        # followed by WHITESPACE ONLY. A `` ```still-content `` line is NOT a
        # closer; treating it as one desynced the scanner and skipped the real
        # receipt. Fail closed: the malformed sequence must not hide the receipt.
        d = self._project("true")
        # Old parser: line 2 opens fence1; line 3 (```+content) wrongly CLOSES it;
        # line 4 (```) then OPENS fence2; the receipt lands inside fence2; the
        # trailing ``` CLOSES fence2 → the receipt is skipped. Correct parser:
        # line 3 is content, line 4 closes fence1, receipt is outside → read.
        body = (
            "# 001 - T\n\n## Status\ndone\n\n## Notes\n"
            "```\n"
            "```still-content-not-a-closer\n"
            "```\n"
            "## Verification Receipt\n\n"
            "### 2026-01-01T00:00:00+00:00 · risk reversible · commit abc1234\n"
            "- **Commands:**\n    - [PASS] `python3 scripts/verify` (verify)\n"
            "```\n")
        self._write_raw_task(d, "001", body)
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "a fence closer with trailing content hid the receipt")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_archived_receipt_still_baselines(self):
        # round-6 panel (sonnet/grok): a strong receipt moved to task-archive.md
        # by `tasks compact` must still count toward the union baseline, or
        # compaction laundered the weakening clean. (compact.py now also refuses
        # to move a receipt; this is the defence-in-depth reader side.)
        d = self._project("true")
        td = self._write_raw_task(d, "001", "# 001 - T\n\n## Status\ndone\n")
        (td / "task-archive.md").write_text(
            "# archived\n\n## Verification Receipt\n\n"
            "### 2026-01-01T00:00:00+00:00 · risk reversible · commit abc1234\n"
            "- **Commands:**\n    - [PASS] `python3 scripts/verify` (verify)\n",
            encoding="utf-8")
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "an archived receipt was dropped from the baseline")
        self.assertEqual(r["status"], "findings")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_upsert_writes_outside_a_fenced_decoy_heading(self):
        # round-9 panel (grok/codex): the reader skips closed fences, but the
        # WRITER (upsert_task_section) matched the first heading fence-blind — so
        # a fenced decoy `## Verification Receipt` made the real close write the
        # receipt INSIDE the fence, where the reader never looks → no baseline.
        # Fence-aware upsert must write the real receipt OUTSIDE the fence so the
        # sweep sees it.
        d = self._project("true")
        td = self._write_raw_task(
            d, "001",
            "# 001 - T\n\n## Status\ndone\n\n## Notes\n"
            "```\n## Verification Receipt\n(decoy example inside a closed fence)\n```\n")
        tf = td / "task.md"
        receipt = format_verify_receipt(
            [("verify", "python3 scripts/verify", 0, "ok")], "abc1234", "reversible",
            timestamp="2026-01-01T00:00:00+00:00")
        upsert_task_section(tf, "Verification Receipt", receipt)
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "upsert wrote the receipt inside the fenced decoy")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_multiline_forced_reason_cannot_break_receipt_parsing(self):
        # round-9 panel (codex): a multi-line --reason written verbatim could
        # inject a `## Notes` line into the receipt, making a section parser exit
        # before the command list → false clean. The reason is collapsed to one
        # line, so the receipt stays parseable and the drop is still flagged.
        d = self._project("true")
        td = self._write_raw_task(d, "001", "# 001 - T\n\n## Status\ndone\n")
        tf = td / "task.md"
        receipt = format_verify_receipt(
            [("verify", "python3 scripts/verify", 0, "ok")], "abc1234", "reversible",
            reason="needed\n## Injected Heading\nmore", timestamp="2026-01-01T00:00:00+00:00")
        # the reason must not introduce a heading line into the receipt
        self.assertNotIn("\n## Injected Heading", receipt)
        upsert_task_section(tf, "Verification Receipt", receipt)
        r = check_verify_contract_change(d)
        self.assertIsNotNone(r, "an injected reason heading truncated the receipt parse")
        self.assertIn("python3 scripts/verify", r["output"])

    def test_run_audit_isolates_a_raising_builtin_check(self):
        # round-8 panel (opus): the built-in checks were called unwrapped, so a
        # raise in one aborted the ENTIRE audit (disabling every other sweep).
        # A raising check must degrade to one error-status result, not crash.
        import tasks.audit as _audit
        d = self._project("true")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])
        orig = _audit.check_verify_contract_change
        try:
            def _boom(_p):
                raise RuntimeError("simulated check crash")
            _audit.check_verify_contract_change = _boom
            res = _audit.run_audit(d)              # must NOT raise
        finally:
            _audit.check_verify_contract_change = orig
        vc = next(r for r in res["results"] if r["name"] == "verify-contract-change")
        self.assertEqual(vc["status"], "error")
        self.assertIn("simulated check crash", vc["output"])
        self.assertFalse(res["passed"])           # a raised check fails the audit
        # the other sweeps still ran (audit was not aborted)
        self.assertIn("conflict-markers", [r["name"] for r in res["results"]])

    def test_run_audit_emits_the_sweep(self):
        # grok panel finding: prove the sweep is actually wired into run_audit.
        d = self._project("true")
        self._add_receipt(d, "001", "2026-01-01T00:00:00+00:00", "reversible",
                          ["python3 scripts/verify"])
        names = [r["name"] for r in run_audit(d)["results"]]
        self.assertIn("verify-contract-change", names)


class VerifyContractJournalAtClose(unittest.TestCase):
    """The close path records the resolved verify command(s) in the enforcement
    journal, so a forensic reader can see what bar each close actually ran."""

    def _repo(self, verify="echo VC_MARKER_9f") -> Path:
        d = Path(tempfile.mkdtemp(prefix="pb-vcj-"))
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        (d / ".agent" / "tasks").mkdir(parents=True)
        (d / ".agent" / "config.json").write_text(
            json.dumps({"verify": verify, "panel_required_for": []}),
            encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            "# 001 - T\n\n## Status\npending\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1: do it\n", encoding="utf-8")
        return d

    def _journal(self, d: Path) -> "list[dict]":
        p = d / ".agent" / "journal" / "enforcement.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in
                p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_close_journals_the_verify_command(self):
        d = self._repo()
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-vcj")
        for k in ("BASH_ENV",):
            env.pop(k, None)
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "done"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        recs = [x for x in self._journal(d) if x.get("hook") == "close"
                and x.get("decision") == "verify-contract"]
        self.assertTrue(recs, "close did not journal a verify-contract record")
        # the verify command is captured specifically in the `command` field —
        # a distinctive marker, so it can't be a coincidental JSON literal.
        self.assertIn("VC_MARKER_9f", recs[-1].get("command", ""))

    def test_close_journals_full_multiline_verify_command(self):
        # round-3 panel (sonnet): a MULTI-LINE verify command was joined with
        # "; " then first-line-truncated by pb_journal._head, dropping every
        # line past the first from the journal record. A single-line-safe
        # encoding must preserve the later lines (within the <PIPE_BUF cap).
        d = self._repo(verify="echo VC_LINE1_7a\necho VC_LINE2_7b")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-vcj3")
        env.pop("BASH_ENV", None)
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "done"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        recs = [x for x in self._journal(d) if x.get("hook") == "close"
                and x.get("decision") == "verify-contract"]
        self.assertTrue(recs, "close did not journal a verify-contract record")
        cmd = recs[-1].get("command", "")
        self.assertNotIn("\n", cmd, "the journal record must stay single-line")
        self.assertIn("VC_LINE1_7a", cmd)
        self.assertIn("VC_LINE2_7b", cmd)   # the later line survived the encoding

    def test_close_journal_command_field_is_bounded(self):
        # round-3 panel (grok): escaping keeps the record single-line, but the
        # `command` field is still capped at pb_journal's 200-BYTE head limit —
        # the <PIPE_BUF atomic single-write bound. This pins that bound honestly
        # (the receipt, not the journal, is the uncapped authoritative list) so
        # the multi-line proof above cannot be read as "captures unbounded text".
        long_cmd = "echo " + "A" * 400          # one command, well over the cap
        d = self._repo(verify=long_cmd)
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-vcj4")
        env.pop("BASH_ENV", None)
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "done"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        recs = [x for x in self._journal(d) if x.get("hook") == "close"
                and x.get("decision") == "verify-contract"]
        self.assertTrue(recs, "close did not journal a verify-contract record")
        self.assertLessEqual(len(recs[-1].get("command", "").encode("utf-8")), 200,
                             "command field must stay within pb_journal's head cap")

    def test_journal_command_field_byte_capped_for_multibyte(self):
        # round-11 panel (codex): the head cap must bound BYTES, not characters —
        # 200 multi-byte chars (emoji) would be ~800 bytes and blow the <PIPE_BUF
        # atomic-write bound the docs/ledger claim. The recorded command field
        # must be <=200 BYTES even for a multi-byte verify command.
        d = self._repo(verify="echo " + "😀" * 300)   # ~4 bytes each, far over 200B
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-vcj5")
        env.pop("BASH_ENV", None)
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "done"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        recs = [x for x in self._journal(d) if x.get("hook") == "close"
                and x.get("decision") == "verify-contract"]
        self.assertTrue(recs, "close did not journal a verify-contract record")
        self.assertLessEqual(len(recs[-1].get("command", "").encode("utf-8")), 200,
                             "multi-byte command field exceeded the byte cap")

    def test_close_still_succeeds_when_journal_is_wedged(self):
        # grok panel finding: the close-time journal is best-effort — a wedged
        # journal (its dir replaced by a FILE so mkdir/open fail) must never
        # break the close.
        d = self._repo()
        # Wedge: make `.agent/journal` a regular file so the journal write fails.
        (d / ".agent" / "journal").write_text("not a dir\n", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-vcj2")
        env.pop("BASH_ENV", None)
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "done"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)


if __name__ == "__main__":
    unittest.main()
