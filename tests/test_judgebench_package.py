#!/usr/bin/env python3
"""Step 3 tests: frozen package builder + historical-leakage filter (plan §9, §20).

The fixture task.md below carries EVERY §20 artifact class, each tagged with a
unique sentinel so one assertion per class proves it is absent from the
reconstructed spec. Over-stripping is acceptable; leaking is not.
"""
import json
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
        self.assertIn("KEEP_PROSE_AFTER_BLANK", out)

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

    def test_allowed_context_names(self):
        for name in ("test-output.txt", "notes.md", "verify.log"):
            self.assertFalse(package.is_denied_context_file(name), name)


if __name__ == "__main__":
    unittest.main()
