#!/usr/bin/env python3
"""Step 3 tests: frozen package builder + historical-leakage filter (plan §9, §20).

The fixture task.md below carries EVERY §20 artifact class, each tagged with a
unique sentinel so one assertion per class proves it is absent from the
reconstructed spec. Over-stripping is acceptable; leaking is not.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.lib import cases, package  # noqa: E402

# One sentinel per §20 class. Each must be ABSENT from the reconstructed spec.
LEAK = {
    "plan_review_section": "LEAK_PLAN_REVIEW_VERDICT_PASS",
    "impl_review_section": "LEAK_IMPL_REVIEW_QUORUM",
    "findings_marker_plan": "LEAK_PLAN_FINDINGS_MARKER_BODY",
    "findings_marker_impl": "LEAK_IMPL_FINDINGS_MARKER_BODY",
    "triage_block": "LEAK_TRIAGE_ACCEPT_R3",
    "handoff_section": "LEAK_HANDOFF_STATE",
    "blocked_section": "LEAK_BLOCKED_REASON",
    "receipt_section": "LEAK_RECEIPT_COMMIT_SHA",
    "debrief_section": "LEAK_DEBRIEF_NOTE",
    "parked_section": "LEAK_PARKED_FROM_PANEL",
    "work_plan_outcome": "LEAK_W1_DONE_NOTE",
    "pre_review_outcome": "LEAK_PREREVIEW_9_OF_9",
    "status_done": "LEAK_STATUS_DONE_VALUE",
    "fenced_heading_body": "LEAK_FENCED_IMPL_REVIEW_BODY",
}

FIXTURE_TASK_MD = f"""# 042 - Demo Task

> **Gate discipline:** One gate → do work → check box → next gate.

## Status
done LEAK_STATUS_DONE_VALUE

> **Before filling this in:** run tasks work.

## Risk
assertive

## Intent
Every judge invocation leaves one spend line. KEEP_INTENT

## Why
Observability. KEEP_WHY

## References
- [x] Context: recalled nodes [8] review.py. KEEP_REFERENCES
- Playbook: playbook/Build

## Design Phase

### Understand
- [x] Restate the request in my own words. — every judge invocation leaves a spend line KEEP_DESIGN_ANSWER
- [x] What is OUT of scope? — a journal reader KEEP_DESIGN_SCOPE

## Plan Review
- [x] Run panel-review — PANEL VERDICT PASS {LEAK['plan_review_section']}
- [x] Triage — accepted F1 {LEAK['triage_block']}

<!-- playbook:plan-review-findings -->
Judge said: {LEAK['findings_marker_plan']}
<!-- /playbook:plan-review-findings -->

---

## Work Plan

> For each work section: what could go wrong?

- [x] **W1 — pb_journal.append_review.** Add a sibling append_review. Check: smoke shows 2 lines. — DONE. {LEAK['work_plan_outcome']} fixed per panel
- [ ] **W2 — review.py emit wiring.** Module helpers plus three emit sites. Check: suite green. KEEP_W2_OPEN
- [x] W3 plain gate with no bold title. Check: red then green. — DONE, 11 tests LEAK_W3_OUTCOME

---

## Implementation Review
- [x] Run panel-review --mode impl — PANEL VERDICT: PASS 4/5 {LEAK['impl_review_section']}

### Triage — impl panel ROUND 3
**ACCEPT** grok #2 {LEAK['triage_block']}

<!-- playbook:impl-review-findings -->
{LEAK['findings_marker_impl']}
<!-- /playbook:impl-review-findings -->

---

## Debrief
- [x] Freehand — {LEAK['debrief_section']}

## Pre-review
- [x] All tests pass — full verify 9/9 {LEAK['pre_review_outcome']}
- [ ] No debug artifacts KEEP_PREREVIEW_OPEN

## Verification Receipt
### 2026-09-01 · risk assertive · commit {LEAK['receipt_section']}

## Handoff
state: {LEAK['handoff_section']}

## Blocked
reason: {LEAK['blocked_section']}

## Parked
- parked per impl-panel grok #3: {LEAK['parked_section']}

## Standing Orders
- Expand dynamically KEEP_OR_DROP_STANDING

```
## Implementation Review
inside a code fence: {LEAK['fenced_heading_body']}
```
"""


class ReconstructSpecTests(unittest.TestCase):
    def setUp(self):
        self.spec = package.reconstruct_spec(FIXTURE_TASK_MD)

    def test_every_leak_class_is_absent(self):
        for cls, sentinel in LEAK.items():
            with self.subTest(cls=cls):
                self.assertNotIn(sentinel, self.spec)
        self.assertNotIn("LEAK_W3_OUTCOME", self.spec)
        self.assertNotIn("LEAK_", self.spec)          # belt and braces

    def test_spec_content_is_kept(self):
        for keep in ("KEEP_INTENT", "KEEP_WHY", "KEEP_REFERENCES", "KEEP_DESIGN_ANSWER",
                     "KEEP_DESIGN_SCOPE", "KEEP_W2_OPEN", "KEEP_PREREVIEW_OPEN",
                     "## Work Plan", "## Design Phase", "## Risk", "assertive"):
            with self.subTest(keep=keep):
                self.assertIn(keep, self.spec)

    def test_status_normalized_to_pending(self):
        lines = self.spec.splitlines()
        i = lines.index("## Status")
        body = [ln for ln in lines[i + 1:i + 4] if ln.strip()]
        self.assertEqual(body[0], "pending")

    def test_work_plan_gates_are_unchecked_and_outcome_free(self):
        self.assertIn("- [ ] **W1 — pb_journal.append_review.** Add a sibling append_review. "
                      "Check: smoke shows 2 lines.", self.spec)
        self.assertNotIn("- [x]", self.spec.split("## Work Plan", 1)[1])
        self.assertIn("- [ ] W3 plain gate with no bold title. Check: red then green.", self.spec)

    def test_design_answers_survive(self):
        # Design gates are pre-review by construction; their answers are the spec.
        self.assertIn("- [x] Restate the request in my own words. — every judge invocation "
                      "leaves a spend line KEEP_DESIGN_ANSWER", self.spec)

    def test_unclosed_findings_marker_drops_to_eof(self):
        text = "## Intent\nKEEP\n<!-- playbook:impl-review-findings -->\nLEAK_TAIL\n## Work Plan\nLEAK_AFTER\n"
        out = package.reconstruct_spec(text)
        self.assertIn("KEEP", out)
        self.assertNotIn("LEAK_TAIL", out)
        self.assertNotIn("LEAK_AFTER", out)

    def test_indented_atx_headings_are_section_boundaries(self):
        # CommonMark allows up to 3 leading spaces before `##` (impl-panel sonnet #1).
        for indent in (" ", "  ", "   "):
            out = package.reconstruct_spec(f"## Intent\nKEEP\n{indent}## Plan Review\nLEAK_INDENTED\n## Work Plan\n- [ ] w\n")
            self.assertNotIn("LEAK_INDENTED", out, repr(indent))
            self.assertIn("KEEP", out)
        # 4 spaces is an indented code block, not a heading — stays in the kept section.
        out = package.reconstruct_spec("## Intent\nKEEP\n    ## not a heading\n")
        self.assertIn("## not a heading", out)

    def test_wrapped_outcome_continuation_lines_are_dropped(self):
        # A checked gate's note may wrap onto indented continuation lines (impl-panel grok F2).
        text = ("## Work Plan\n"
                "- [x] **W1 — thing.** Do it. Check: x. — DONE. first line of note\n"
                "  LEAK_WRAPPED_NOTE the panel found extract_risk shadowing\n"
                "  LEAK_WRAPPED_TWO\n"
                "- [ ] **W2 — open.** KEEP_W2\n"
                "  KEEP_W2_CONTINUATION (an open gate's own wrapped text)\n"
                "\n"
                "KEEP_PROSE_AFTER_BLANK\n")
        out = package.reconstruct_spec(text)
        self.assertNotIn("LEAK_WRAPPED", out)
        self.assertIn("- [ ] **W1 — thing.** Do it. Check: x.", out)
        self.assertIn("KEEP_W2_CONTINUATION", out)
        # Since task 048 impl-panel round 2 a plain paragraph inside Work Plan is DROPPED (execution
        # results live there; over-strip is the safe direction) — the earlier expectation is inverted.
        self.assertNotIn("KEEP_PROSE_AFTER_BLANK", out)

    def test_idempotent(self):
        self.assertEqual(package.reconstruct_spec(self.spec), self.spec)

    def test_heading_variants_are_cut(self):
        # Allowlist semantics: known review sections AND any unknown heading are cut.
        for heading in ("## Plan Review", "##  Implementation Review ##", "## Verification Receipts",
                        "## Verification Receipt", "## TRIAGE", "## Handoff (auto)", "## Handoff",
                        "## Blocked", "## Parked", "## Debrief", "## Standing Orders",
                        "## Retro notes (never seen before)", "## Work Plan Review"):
            with self.subTest(heading=heading):
                out = package.reconstruct_spec(f"## Intent\nKEEP\n{heading}\nLEAK_X\n")
                self.assertIn("KEEP", out)
                self.assertNotIn("LEAK_X", out)


class LeakScanTests(unittest.TestCase):
    """impl-panel r2 opus F2: kept sections pass through verbatim, so a review-informed
    edit inside Intent/Why/References/Design (or an H3 review note there) must be caught
    by a final fail-loud scan over the rendered spec, not silently frozen."""

    def test_scan_flags_review_tokens_in_kept_sections(self):
        # These survive reconstruction (they sit inside a KEPT section) and must be flagged.
        for body in ("Intent text — revised per PANEL VERDICT: PASS",
                     "### Implementation Review notes\nthe impl panel said X",
                     "kept per impl-panel grok #3",
                     "fixed per impl-panel F2",
                     "CAP: 5/5 reported, exhausted",
                     "triage: accept F1, reject F2"):
            with self.subTest(body=body[:30]):
                spec = package.reconstruct_spec(f"## Intent\n{body}\n")
                self.assertIn(body.split("\n")[0][:20], spec)          # really kept …
                self.assertTrue(package.leak_scan(spec), body)          # … and really flagged
        # The scan itself also knows the structural tokens the filter normally removes.
        for raw in ("<!-- playbook:impl-review-findings -->", "## Triage — round 2"):
            self.assertTrue(package.leak_scan(raw), raw)

    def test_scan_is_quiet_on_a_clean_spec(self):
        self.assertEqual(package.leak_scan(package.reconstruct_spec(FIXTURE_TASK_MD)), [])
        self.assertEqual(package.leak_scan("## Intent\nBuild a plan for the review of PRs.\n"), [])

    def test_scan_tolerates_legitimate_pre_review_plan_text(self):
        # r3 opus F2: real Work Plans PLAN the panel — that is design text, not a leak.
        for body in ("- [ ] W0: run the impl panel in the background; triage when judge.md lands. "
                     "Check: judge.md has a PANEL VERDICT.",
                     "- [ ] Plan-review panel launched; fix findings before W1.",
                     "The judge reads judge.md; the panel verdict gates the close."):
            with self.subTest(body=body[:30]):
                self.assertEqual(package.leak_scan(body), [], body)

    def test_build_package_fails_loud_on_a_leaky_kept_section(self):
        with tempfile.TemporaryDirectory() as td:
            case = _mk_case(Path(td))
            case.spec_path.write_text("## Intent\nfixed per PANEL VERDICT: PASS 5/5\n", encoding="utf-8")
            with self.assertRaisesRegex(package.LeakageError, "PANEL VERDICT"):
                package.build_package(case)


class StripOutcomeTests(unittest.TestCase):
    def test_bold_title_keeps_its_own_dash(self):
        self.assertEqual(package.strip_outcome("- [x] **W1 — title.** body. — DONE. note\n"),
                         "- [ ] **W1 — title.** body.\n")

    def test_no_bold_cuts_at_first_dash(self):
        self.assertEqual(package.strip_outcome("- [x] gate text — outcome — more"),
                         "- [ ] gate text")

    def test_unchecked_untouched(self):
        line = "- [ ] open gate — with a dash in spec\n"
        self.assertEqual(package.strip_outcome(line), line)

    def test_non_gate_untouched(self):
        line = "plain prose — with dash\n"
        self.assertEqual(package.strip_outcome(line), line)

    def test_checked_without_outcome(self):
        self.assertEqual(package.strip_outcome("  - [X] nested gate\n"), "  - [ ] nested gate\n")


class TemplateTests(unittest.TestCase):
    def test_template_v1_stamp_and_placeholders(self):
        text, version, sha = package.load_template()
        self.assertEqual(version, "v1")
        self.assertEqual(len(sha), 64)
        for token in ("{{SPEC}}", "{{DIFF}}", "{{CONTEXT}}", "{{TIME_BUDGET}}"):
            self.assertIn(token, text)
        # Framed on the production impl-review prompt (plan-review panel, opus):
        # same lenses, same adversarial stance, findings block is a TRAILING summary.
        for phrase in ("senior engineer", "six lenses", "Hostile sequences", "Test quality",
                       "Prove it works", "adversarial", "as the LAST thing you",
                       "Stay inside the repository"):
            self.assertIn(phrase, text)
        self.assertLess(text.index("six lenses"), text.index("FINDINGS:"))
        # The parseable output contract the findings parser (step 4) relies on.
        for line in ("FINDINGS:", "END FINDINGS", "FILE:", "SYMBOL:",
                     "SEVERITY: <Critical|Important|Minor>", "WHY:", "NONE"):
            self.assertIn(line, text)
        self.assertIn("Critical", text); self.assertIn("Important", text); self.assertIn("Minor", text)

    def test_render_substitutes_all_and_orders_sections(self):
        text, _, _ = package.load_template()
        out = package.render_prompt("SPEC_BODY", "DIFF_BODY", [("context/t.txt", "CTX_BODY")], text)
        for tok in ("{{SPEC}}", "{{DIFF}}", "{{CONTEXT}}"):
            self.assertNotIn(tok, out)
        self.assertLess(out.index("SPEC_BODY"), out.index("DIFF_BODY"))
        self.assertLess(out.index("DIFF_BODY"), out.index("CTX_BODY"))
        self.assertIn("--- context/t.txt ---", out)

    def test_render_without_context_has_no_context_block(self):
        text, _, _ = package.load_template()
        out = package.render_prompt("S", "D", [], text)
        self.assertNotIn("FROZEN CONTEXT", out)
        self.assertNotIn("{{TIME_BUDGET}}", out)

    def test_time_budget_clause_reuses_production_wording(self):
        self.assertEqual(package.time_budget_clause(None, None), "")
        clause = package.time_budget_clause(900, 1200)
        self.assertIn("15", clause)              # 900 s → 15 min, production's human_duration
        text, _, _ = package.load_template()
        out = package.render_prompt("S", "D", [], text, time_budget=clause)
        self.assertIn(clause, out)

    def test_render_is_single_pass_over_the_template(self):
        # r3 grok #4: a spec/diff that CONTAINS a placeholder token must stay literal.
        text, _, _ = package.load_template()
        out = package.render_prompt("spec mentions {{DIFF}} and {{CONTEXT}}", "diff has {{SPEC}} {{TIME_BUDGET}}",
                                    [("c.txt", "ctx {{SPEC}}")], text, time_budget="tb {{DIFF}}")
        self.assertEqual(out.count("spec mentions {{DIFF}} and {{CONTEXT}}"), 1)
        self.assertEqual(out.count("diff has {{SPEC}} {{TIME_BUDGET}}"), 1)
        self.assertEqual(out.count("ctx {{SPEC}}"), 1)
        self.assertEqual(out.count("tb {{DIFF}}"), 1)

    def test_render_rejects_template_missing_placeholder(self):
        with self.assertRaises(ValueError):
            package.render_prompt("S", "D", [], "no placeholders here")


SHA = "0123456789abcdef0123456789abcdef01234567"


def _mk_case(root: Path, cid="pb-042-demo", context=None):
    d = root / "cases" / cid
    d.mkdir(parents=True)
    (d / "case.json").write_text(json.dumps({
        "id": cid, "source": {"workspace": "w", "task": "042", "repo": "r"},
        "repo_base_sha": SHA, "diff_of": SHA[:7], "kind": "feature", "area": "enforcement",
        "difficulty": "medium", "truth_version": 1}), encoding="utf-8")
    (d / "truth.json").write_text(json.dumps({"findings": [], "known_rejects": []}), encoding="utf-8")
    (d / "spec.md").write_text(FIXTURE_TASK_MD, encoding="utf-8")
    (d / "diff.patch").write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+DIFF_LINE_B\n", encoding="utf-8")
    for name, body in (context or {}).items():
        p = d / "context" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    (root / "corpus.json").write_text(json.dumps({"version": 1, "cases": [cid]}), encoding="utf-8")
    return cases.load_corpus(root).get(cid)


class BuildPackageTests(unittest.TestCase):
    def test_end_to_end_package_is_leak_free_and_complete(self):
        with tempfile.TemporaryDirectory() as td:
            case = _mk_case(Path(td), context={"test-output.txt": "CTX_TEST_OUTPUT"})
            pkg = package.build_package(case)
            self.assertEqual(pkg.case_id, "pb-042-demo")
            self.assertEqual(pkg.template_version, "v1")
            self.assertNotIn("LEAK_", pkg.prompt)
            for keep in ("KEEP_INTENT", "DIFF_LINE_B", "CTX_TEST_OUTPUT", "FINDINGS:"):
                self.assertIn(keep, pkg.prompt)
            self.assertEqual(pkg.context, [("context/test-output.txt", "CTX_TEST_OUTPUT")])
            self.assertGreater(pkg.prompt_chars, 0)

    def test_denied_context_artifacts_fail_loud(self):
        for name in ("judge.md", "judge-archive.md", "judge-opus.log", "judge-x.partial.log",
                     "task-archive.md", "vetting-ledger.json", "truth.json", "case.json",
                     "MIND_MAP.md", "MIND_MAP_OVERFLOW.md", "chat_log.md", "enforcement.jsonl",
                     "nested/judge.md"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as td:
                    case = _mk_case(Path(td), context={name: "LEAK_CONTEXT"})
                    with self.assertRaises(package.LeakageError):
                        package.build_package(case)

    def test_denied_names_are_case_insensitive_on_every_platform(self):
        # fnmatch is case-sensitive on POSIX (impl-panel sonnet #2) — the guard must not be.
        for name in ("Truth.json", "JUDGE.MD", "Judge-Archive.md", "mind_map.md", "Task-Archive.MD",
                     "Vetting-Ledger.json", "CASE.json", "Enforcement.JSONL"):
            self.assertTrue(package.is_denied_context_file(name), name)

    @unittest.skipIf(not hasattr(os, "symlink"), "no symlinks here")
    def test_symlinked_context_files_are_refused(self):
        # impl-panel r2 terra #4: `context/reference.txt -> ../truth.json` would carry a
        # denied artifact under an innocent basename.
        with tempfile.TemporaryDirectory() as td:
            case = _mk_case(Path(td), context={"notes.md": "ok"})
            link = case.context_dir / "reference.txt"
            try:
                os.symlink(case.truth_path, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink not permitted on this host")
            with self.assertRaisesRegex(package.LeakageError, "symlink"):
                package.build_package(case)

    def test_allowed_context_names(self):
        for name in ("test-output.txt", "notes.md", "verify.log"):
            self.assertFalse(package.is_denied_context_file(name), name)


if __name__ == "__main__":
    unittest.main()


class ImplPanelRound1Tests(unittest.TestCase):
    """Task 048 impl-panel round 1 (opus #1 / codex:sol #1): kept sections still carried
    review provenance — Recent Chat blocks, `(panel opus#4/terra#4)` parentheticals, bare
    seat#N refs, ACCEPT-<label> tags, `→` gate outcomes, execution `**Result:**` bullets —
    and `leak_scan` false-passed them. Redaction is mechanical (deterministic, over-strips)
    and the scan is broadened for what redaction cannot decide."""

    MD = """# 007 - X

## Intent
Add foo.

## References
- Context: node [3]

### Recent Chat (auto-captured at activation — review and remove unrelated)

**[M245]** [2026-08-27 21:43:12]
OWNER RULING — stop the panel loop and close. LEAK_CHAT

---

## Design Phase
- [x] Restate → sum the list. LEAK_ARROW_DESIGN_KEEP

## Work Plan
- **Fail-closed validation** (panel sol#3/terra#4, matches `required()`): absent var ⇒ default LEAK_PAREN
- **Cache tag** (panel opus#1/terra-crit/sol#2): a tag KEEP_TAG
- Proven by LITERAL tests (panel sonnet#1/#2), not self-referential ones KEEP_LIT
- bare ref opus#2 and codex:sol#4 and terra-crit#1 here KEEP_BARE
**Item 2 — race guards (red-first; ACCEPT-C, rule 5 — 3 sites) KEEP_ITEM**
- [ ] W9: honest copy (ACCEPT-B decision). KEEP_W9
- [x] **W1 — implement.** Write the loop. → **PANEL PASS 5/5** LEAK_ARROW_OUTCOME
- **Result:** measured 15 fps LEAK_RESULT
- **Note:** planning remark KEEP_NOTE
"""

    def test_recent_chat_block_is_dropped(self):
        spec = package.reconstruct_spec(self.MD)
        self.assertNotIn("LEAK_CHAT", spec)
        self.assertNotIn("Recent Chat", spec)
        self.assertIn("- Context: node [3]", spec)          # the rest of References survives
        self.assertIn("## Design Phase", spec)

    def test_panel_parentheticals_and_seat_refs_are_redacted(self):
        spec = package.reconstruct_spec(self.MD)
        for leak in ("panel sol#3", "terra#4", "opus#1", "terra-crit", "sonnet#1", "opus#2", "codex:sol#4"):
            self.assertNotIn(leak, spec, leak)
        for keep in ("KEEP_TAG", "KEEP_LIT", "KEEP_BARE", "matches `required()`", "absent var"):
            self.assertIn(keep, spec, keep)

    def test_accept_labels_are_redacted(self):
        spec = package.reconstruct_spec(self.MD)
        self.assertNotIn("ACCEPT-", spec)
        self.assertIn("KEEP_ITEM", spec)
        self.assertIn("KEEP_W9", spec)
        self.assertIn("rule 5", spec)

    def test_arrow_gate_outcome_is_stripped_but_design_arrow_answer_kept(self):
        spec = package.reconstruct_spec(self.MD)
        self.assertNotIn("LEAK_ARROW_OUTCOME", spec)
        self.assertNotIn("PANEL PASS", spec)
        self.assertIn("- [ ] **W1 — implement.** Write the loop.", spec)
        self.assertIn("LEAK_ARROW_DESIGN_KEEP", spec)       # Design answers are pre-review

    def test_result_bullets_in_work_plan_are_dropped(self):
        spec = package.reconstruct_spec(self.MD)
        self.assertNotIn("LEAK_RESULT", spec)
        self.assertIn("KEEP_NOTE", spec)

    def test_broadened_leak_scan_catches_the_panel_classes(self):
        for text in ("see (panel opus#4/terra#4) here", "added at impl-review the cache",
                     "> Revised after plan-review round 1.", "status **FIXED (round 2-4)** now",
                     "bar is HARDENED (r3)", "BEHAVIORAL (finding H, surface)", "OWNER RULING — stop the panel loop",
                     "### Recent Chat (auto)", "red-first; ACCEPT-C, rule 5", "bare terra#4 ref"):
            self.assertTrue(package.leak_scan(text), text)
        for text in ("run the plan review then build", "the impl panel reviews the tree", "finding the bug",
                     "round trip latency", "accept the input", "we PARK the car",
                     "Owner ruling 9 (MIND_MAP [0], 2026-08-17): the product becomes self-hosted"):
            self.assertEqual(package.leak_scan(text), [], text)

    def test_plan_finding_letter_parentheticals_are_redacted(self):
        md = "## Work Plan\n- **W1** the LITERAL set only; the ledger is BEHAVIORAL (finding H, surface to owner). **W4** add a value (finding G). Also (findings A, B, C) here. Keep finding the bug.\n"
        spec = package.reconstruct_spec(md)
        for leak in ("finding H", "finding G", "findings A"):
            self.assertNotIn(leak, spec, leak)
        self.assertIn("Keep finding the bug.", spec)
        self.assertIn("the ledger is BEHAVIORAL. **W4** add a value. Also here.", spec)

    def test_reconstruct_spec_is_idempotent_and_clean_on_the_fixture(self):
        once = package.reconstruct_spec(self.MD)
        self.assertEqual(package.reconstruct_spec(once), once)
        self.assertEqual(package.leak_scan(once), [])


class ImplPanelRound2Tests(unittest.TestCase):
    """Task 048 impl-panel round 2 (codex:sol/terra Critical, grok, opus/sonnet): Work Plan
    BODY text — separator-less checked gates, tables, result paragraphs, bold continuations
    — carried execution outcomes; a dangling `panel )` survived seat-ref removal."""

    MD = """# 011 - X

## Intent
Self-host.

## Work Plan
> **Shape:** Phase A → proof → CHECKPOINT. KEEP_QUOTE

### Phase A — author the stack
- [x] W2. [P5] `fetch.sh` written. Pins the DATED extract 260824, md5 `abc` verified LEAK_NOSEP_BODY
- [x] W8. [P8] **Byte-identity proven.** Working tree: only `M .gitignore` LEAK_BOLD_NOSEP
- [x] **W1 — implement foo.** Write the loop carefully. — DONE, 12 tests LEAK_SEP_NOTE
- [ ] W3. Authored `compose.yml` (SEPARATE file). KEEP_OPEN_GATE
**Item 2 — race guards (red-first, rule 5 — 3 sites) KEEP_BOLD_ONLY**
**Design decisions (locked before coding) KEEP_BOLD_COLON:**
- **Provider hosts** → read inside handlers (span > 2° either axis (single-city cap, panel opus#2)). KEEP_HOSTS
- [x] W15. **Parity measured** LEAK_PARITY_TITLE_OK

 | metric | origin | verdict |
 |---|---|---|
 | walk | Unirii | PASS | LEAK_TABLE

 **17/18 ring metrics exact** (radial 0.0%) LEAK_RESULT_PARA
plain paragraph with measured 24 min import LEAK_PLAIN_PARA
- [x] W9. [P4] **Reassessed.** Sizes stated; plan panel triaged (P1–P10) folded in. LEAK_TRIAGED
**bold continuation of a checked gate LEAK_BOLD_CONT**
- W4. plan bullet without a checkbox KEEP_BULLET
  indented continuation of an OPEN bullet KEEP_INDENT
"""

    def test_separator_less_checked_gate_keeps_only_its_title(self):
        spec = package.reconstruct_spec(self.MD)
        self.assertNotIn("LEAK_NOSEP_BODY", spec)
        self.assertNotIn("md5", spec)
        self.assertIn("- [ ] W2. [P5] `fetch.sh` written", spec)          # first clause survives as the title
        self.assertNotIn("LEAK_BOLD_NOSEP", spec)
        self.assertIn("- [ ] W8. [P8] **Byte-identity proven.**", spec)
        self.assertNotIn("LEAK_SEP_NOTE", spec)
        self.assertIn("- [ ] **W1 — implement foo.** Write the loop carefully.", spec)   # separator rule unchanged
        self.assertIn("KEEP_OPEN_GATE", spec)

    def test_tables_and_result_paragraphs_in_work_plan_are_dropped(self):
        spec = package.reconstruct_spec(self.MD)
        for leak in ("LEAK_TABLE", "| metric |", "LEAK_RESULT_PARA", "LEAK_PLAIN_PARA", "LEAK_BOLD_CONT"):
            self.assertNotIn(leak, spec, leak)
        for keep in ("KEEP_QUOTE", "KEEP_BOLD_ONLY", "KEEP_BOLD_COLON", "KEEP_HOSTS", "KEEP_BULLET", "KEEP_INDENT",
                     "### Phase A — author the stack"):
            self.assertIn(keep, spec, keep)

    def test_dangling_panel_word_and_triaged_clause_are_redacted(self):
        spec = package.reconstruct_spec(self.MD)
        self.assertNotIn("panel )", spec)
        self.assertNotIn("opus#2", spec)
        self.assertIn("(single-city cap)", spec)
        self.assertNotIn("LEAK_TRIAGED", spec)          # the W9 body goes with the title-only rule
        self.assertNotIn("triaged", spec)
        self.assertEqual(package.leak_scan(spec), [], spec)
        self.assertEqual(package.reconstruct_spec(spec), spec)

    def test_long_untitled_checked_gate_keeps_only_its_first_clause_even_with_a_late_separator(self):
        md = ("## Work Plan\n- [x] W2. [P5] `fetch.sh` written. Pins the DATED extract; verifies the published md5 "
              "(`abc`) and refuses a corrupt file — Session C note LEAK_LATE\n- [x] short gate — done\n")
        spec = package.reconstruct_spec(md)
        self.assertIn("- [ ] W2. [P5] `fetch.sh` written\n", spec)
        self.assertNotIn("LEAK_LATE", spec)
        self.assertIn("- [ ] short gate\n", spec)

    def test_status_table_cells_are_leak_tokens(self):
        self.assertTrue(package.leak_scan("| `set_task_blocked` | core.py:2192 | **FIXED** | now via x |"))
        self.assertTrue(package.leak_scan("| x | **HARDENED** |"))
        self.assertEqual(package.leak_scan("the bug is fixed in this diff"), [])

    def test_leak_scan_catches_dangling_panel_and_triaged(self):
        for text in ("cap, panel ).", "the (panel) said", "plan panel triaged (P1–P10)", "items triaged above"):
            self.assertTrue(package.leak_scan(text), text)
        for text in ("a panel of experts", "the panel-review gate", "solar panel"):
            self.assertEqual(package.leak_scan(text), [], text)
