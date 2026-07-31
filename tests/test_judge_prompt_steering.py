#!/usr/bin/env python3
"""Point tests for the two steering blocks in the blind judge prompts.

Both exist because a judge left to its own devices spends its budget the wrong
way — either exploring until something kills it mid-sentence, or writing a long
report off shallow analysis.

  * TIME BUDGET  — how long you have, and what to do as it runs out.
  * DEPTH        — how hard to think before writing, and that the thinking stays
                   internal while the report stays terse.

Both must reach all four builders. There are four rather than one because the
task.md-editing pair and the stdout-only panel pair are separate functions, and a
paragraph added to "the review prompt" has historically landed in two of them.

Why this file asserts on source text as well as rendered output: the canonical
`tasks/template.py` carries the full timeout API, while
`scripts/lib/tasks/template.py` is the dead mirror (pinned at VERSION 1.4.1 by
test_version_parity, documented as parked for deletion in provider/paths.py) and
deliberately carries only the prompt text. So the depth block is checked in both
copies by text, and the time paragraph is rendered only from the canonical copy.

Pure stdlib unittest — honors the stdlib-only runtime invariant.
Run: python3 tests/test_judge_prompt_steering.py
"""
from __future__ import annotations

import ast
import importlib
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = REPO_ROOT / "plugins" / "playbook"
CANONICAL = PLAYBOOK / "tasks" / "template.py"
MIRROR = PLAYBOOK / "scripts" / "lib" / "tasks" / "template.py"

PLAN_BUILDERS = ("plan_review_prompt", "panel_plan_review_prompt")
IMPL_BUILDERS = ("impl_review_prompt", "panel_impl_review_prompt")
ALL_BUILDERS = PLAN_BUILDERS + IMPL_BUILDERS

sys.path.insert(0, str(PLAYBOOK))
from tasks import template as canonical_template  # noqa: E402


def builder_sources(path: Path) -> dict[str, str]:
    """name -> unparsed source, so an assertion can name WHICH builder is short."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name: ast.unparse(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }


class DepthBlockTest(unittest.TestCase):
    """Theme B: reason deeply, report tersely — in every builder, both copies."""

    # Phrases distinctive enough that they cannot pass by accident on upstream.
    SHARED = (
        "Work the problem deeply before you write anything",
        "spend substantial reasoning effort on the analysis, not on a long report",
        "All of that reasoning stays internal",
        "Depth of thinking, brevity of report.",
        "do not treat lack of file access as a reason to drop everything",
    )
    PLAN_ONLY = "independent hypotheses about where this plan will fail or fall short"
    IMPL_ONLY = "independent hypotheses about how this code could be wrong"
    TEST_CLAIM = "check the test would actually fail if the behavior regressed"

    def test_shared_depth_phrases_in_every_builder_of_both_copies(self):
        for path in (CANONICAL, MIRROR):
            funcs = builder_sources(path)
            for name in ALL_BUILDERS:
                self.assertIn(name, funcs, f"{path.name}: {name} missing")
                for phrase in self.SHARED:
                    with self.subTest(copy=path.name, builder=name, phrase=phrase[:40]):
                        self.assertIn(
                            phrase, funcs[name],
                            f"{path.relative_to(REPO_ROOT)}: {name} lacks the depth block",
                        )

    def test_plan_and_impl_flavours_do_not_cross_over(self):
        """The two flavours ask for different hypotheses; pasting one everywhere
        would tell a plan reviewer to hunt for races in code that doesn't exist."""
        for path in (CANONICAL, MIRROR):
            funcs = builder_sources(path)
            for name in PLAN_BUILDERS:
                with self.subTest(copy=path.name, builder=name):
                    self.assertIn(self.PLAN_ONLY, funcs[name])
                    self.assertNotIn(self.IMPL_ONLY, funcs[name])
                    self.assertNotIn(self.TEST_CLAIM, funcs[name])
            for name in IMPL_BUILDERS:
                with self.subTest(copy=path.name, builder=name):
                    self.assertIn(self.IMPL_ONLY, funcs[name])
                    self.assertIn(self.TEST_CLAIM, funcs[name])
                    self.assertNotIn(self.PLAN_ONLY, funcs[name])

    def test_depth_block_precedes_the_lens_list(self):
        """Order is the instruction: think first, then apply the lenses."""
        for name in ALL_BUILDERS:
            rendered = getattr(canonical_template, name)("a/001-x/task.md")
            with self.subTest(builder=name):
                depth = rendered.index("Work the problem deeply")
                lenses = rendered.index("lenses: ")
                self.assertLess(depth, lenses, f"{name}: depth block after the lenses")

    def test_lens_count_wording_untouched(self):
        """The depth block is inserted before the lens list, never into it — a
        stale "six lenses" against seven items is exactly what
        test_review_prompt_lenses guards, and this block must not disturb it."""
        for name in ALL_BUILDERS:
            rendered = getattr(canonical_template, name)("a/001-x/task.md")
            with self.subTest(builder=name):
                self.assertEqual(rendered.count("six lenses"), 1)


class TimeBudgetInstructionTest(unittest.TestCase):
    """The soft/hard time paragraph (added with the soft-timeout split).

    Rendered from the canonical copy only — see the module docstring.
    """

    def test_absent_without_a_soft_budget(self):
        """No soft deadline configured means no time paragraph at all, rather
        than a paragraph about a hard kill the judge should never aim for."""
        self.assertEqual(canonical_template.time_budget_instruction(None, 1200), "")
        for name in ALL_BUILDERS:
            with self.subTest(builder=name):
                self.assertNotIn(
                    "TIME BUDGET", getattr(canonical_template, name)("a/001-x/task.md"))

    def test_present_in_every_builder_with_a_soft_budget(self):
        for name in ALL_BUILDERS:
            rendered = getattr(canonical_template, name)(
                "a/001-x/task.md", soft_timeout_secs=900, hard_timeout_secs=1200)
            with self.subTest(builder=name):
                self.assertIn("TIME BUDGET — soft deadline ~15 minutes", rendered)
                self.assertIn("hang safety at ~20 minutes", rendered)

    def test_seconds_are_rendered_as_readable_durations(self):
        text = canonical_template.time_budget_instruction(900, 1200)
        self.assertIn("~15 minutes", text)
        self.assertNotIn("900", text)
        self.assertIn("~90s", canonical_template.time_budget_instruction(90, None))

    def test_unlimited_hard_does_not_advertise_a_kill_time(self):
        text = canonical_template.time_budget_instruction(900, None)
        self.assertIn("no hard process kill configured", text)
        self.assertNotIn("hang safety", text)

    def test_the_paragraph_names_what_to_do_at_the_deadline(self):
        """The point of a soft deadline is the behaviour it asks for; a bare
        number would just be a countdown."""
        text = canonical_template.time_budget_instruction(900, 1200)
        for phrase in (
            "finish the SINGLE thought process you are currently in",
            "write your final findings",
            "Do NOT start a new hypothesis",
            "A shorter fully-grounded report beats a longer incomplete one",
        ):
            with self.subTest(phrase=phrase[:40]):
                self.assertIn(phrase, text)

    def test_builders_accept_the_kwargs_positionally_free(self):
        """The kwargs are keyword-only, so an existing two-positional-arg call
        keeps working — every pre-existing caller passes (task_path, inline)."""
        for name in ALL_BUILDERS:
            with self.subTest(builder=name):
                getattr(canonical_template, name)("a/001-x/task.md", True)

    def test_legacy_judge_prompt_alias_forwards_the_budgets(self):
        for mode in ("plan", "impl"):
            with self.subTest(mode=mode):
                self.assertIn(
                    "TIME BUDGET",
                    canonical_template.judge_prompt(
                        "a/001-x/task.md", mode=mode, soft_timeout_secs=900),
                )


if __name__ == "__main__":
    unittest.main()
