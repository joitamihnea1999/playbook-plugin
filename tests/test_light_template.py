#!/usr/bin/env python3
"""The `light` task shape (F14): ceremony compressed, review non-negotiable.

Field evidence (batch 3): a ~20-line doc note dragged the 32-gate Build
template because `quick` has no review gates and assertive work must be
reviewed (the 056 lesson: a docs-only diff that changed zero code made the
project's biggest false claim). `light` is the middle shape: ~6 gates, risk
classified FIRST with a written why, review routed by risk.

The blind judge FAILED the first design with three concrete skip-review
routes; every control here exists because of that verdict:

  * the close-side bar is real for light: an assertive light task with no
    impl evidence cannot close (default config), and under
    `panel_required_for: "all"` single-judge TEXT evidence is not enough —
    the panel bar is structural;
  * the rendered template itself cannot mint review evidence: with EVERY
    gate checked, `has_review_evidence(impl_only=True)` stays False (the
    judge caught gate wording whose substrings satisfied the matcher);
  * the switch path can no longer close ANYTHING silently: `tasks work <N>`
    used to write `done` directly on a fully-gated previous task — no risk
    check, no verify, no receipt (judge Finding 1, a pre-existing hole in
    the evidence contract for every shape). It now bounces to
    `tasks work done`; `--force` leaves the task honestly open;
  * `unclassified` risk closes with a LOUD warning (not silently), and a
    malformed one-line `## Risk: assertive` parses as unclassified —
    warned, named, tested.

Config assumptions are explicit per test: default = no panel_required_for.

Run: python3 -m unittest tests.test_light_template
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

from tasks.core import has_review_evidence  # noqa: E402
from tasks.template import render_template  # noqa: E402

SESSION = "pid-light-test"

PANEL_IMPL_ROUND = ("# Panel Impl Review — task {n}\n\n"
                    "**PANEL VERDICT: PASS** — 4/4 judges, quorum 3\n"
                    f"**Commit:** {'a' * 40}\n"
                    "**Tree-state:** abc123def456\n\nfindings body\n")


class Fixture:
    def __init__(self, config: "dict | None" = None):
        self.proj = Path(tempfile.mkdtemp()) / "proj"
        (self.proj / ".agent" / "tasks").mkdir(parents=True)
        if config is not None:
            (self.proj / ".agent" / "config.json").write_text(
                json.dumps(config), encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONPATH=str(PLUGIN),
                   PLAYBOOK_SESSION_ID=SESSION)
        return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                              cwd=self.proj, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=120)

    def make_light_task(self, risk: "str | None", *, checked: bool,
                        judge_md: "str | None" = None,
                        malformed_risk_line: bool = False,
                        strip_risk_section: bool = False) -> Path:
        r = self.run("new", "light", "small change")
        assert r.returncode == 0, r.stderr
        tf = next((self.proj / ".agent" / "tasks").glob("001-*/task.md"))
        text = tf.read_text(encoding="utf-8")
        if risk is not None:
            text = text.replace("## Risk\nunclassified", f"## Risk\n{risk}", 1)
        if malformed_risk_line:
            text = text.replace("## Risk\nunclassified", "## Risk: assertive\n", 1)
        if strip_risk_section:
            # Stand in for a pre-1.5.0 template: no Risk heading at all.
            text = text.replace("## Risk\nunclassified", "", 1)
        if checked:
            text = text.replace("- [ ]", "- [x]")
        tf.write_text(text, encoding="utf-8")
        if judge_md is not None:
            (tf.parent / "judge.md").write_text(judge_md, encoding="utf-8")
        return tf


def status_of(tf: Path) -> str:
    lines = tf.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Status" and i + 1 < len(lines):
            return lines[i + 1].strip()
    return "?"


class RenderedShape(unittest.TestCase):
    def setUp(self):
        self.text = render_template(1, "Doc Note", "light")

    def test_risk_block_and_routing_gate_first(self):
        self.assertIn("## Risk\nunclassified", self.text)
        gates = [l for l in self.text.splitlines() if l.startswith("- [ ]")]
        self.assertTrue(gates[0].startswith("- [ ] `## Risk` above set"),
                        gates[0])
        self.assertLessEqual(len(gates), 8, f"{len(gates)} gates is not light")

    def test_review_gate_present_and_routes_by_risk(self):
        self.assertIn("## Review", self.text)
        self.assertIn("implementation panel", self.text)
        self.assertIn("light never waives review", self.text.lower())

    def test_fully_checked_template_cannot_mint_review_evidence(self):
        # Judge Finding 2: the first design's gate wording satisfied
        # has_review_evidence by substring. The rendered shape with every box
        # checked must still show NO impl-grade evidence.
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text(self.text.replace("- [ ]", "- [x]"), encoding="utf-8")
        self.assertFalse(has_review_evidence(tf, impl_only=True),
                         "the light template itself mints review evidence")

    def test_selection_rule_on_both_stickers(self):
        self.assertIn("quick", self.text.lower())
        quick = render_template(1, "Trivia", "quick")
        self.assertIn("light", quick.lower(),
                      "quick's sticker must carry the selection rule too")

    def test_born_checked_guidance_on_every_sticker(self):
        # S3 (1.5.41): `born-checked` is the single most frequent block —
        # agents keep adding ALREADY-checked gates to record completed work.
        # Every gate-discipline sticker must carry the rule where agents read
        # it: write the gate BEFORE the step, a gate may never be born checked,
        # and to record already-done work add it unchecked then check it in a
        # SEPARATE edit with its outcome. Pins the guidance text so it cannot
        # silently regress.
        for ttype in ("build", "light", "quick"):
            text = render_template(1, "X", ttype).lower()
            self.assertIn("born checked", text,
                          f"{ttype} sticker missing the born-checked rule")
            self.assertIn("separate edit", text,
                          f"{ttype} sticker missing how to record already-done work")


class SkillGuidance(unittest.TestCase):
    def test_playbook_skill_carries_born_checked_guidance(self):
        # The playbook skill is the OTHER place agents read gate rules; the
        # same born-checked guidance must live there too (S3).
        skill = (PLUGIN / "skills" / "playbook" / "SKILL.md").read_text(
            encoding="utf-8").lower()
        self.assertIn("born checked", skill)
        self.assertIn("separate edit", skill)


class CloseSideBar(unittest.TestCase):
    """Default config (no panel_required_for) unless stated."""

    def test_assertive_light_without_evidence_cannot_close(self):
        f = Fixture()
        tf = f.make_light_task("assertive", checked=True)
        self.assertEqual(f.run("work", "1").returncode, 0)
        r = f.run("work", "done")
        self.assertEqual(r.returncode, 1,
                         "assertive light task closed without review:\n" + r.stdout)
        self.assertNotEqual(status_of(tf), "done")

    def test_assertive_light_with_panel_evidence_closes(self):
        f = Fixture()
        tf = f.make_light_task("assertive", checked=True,
                               judge_md=PANEL_IMPL_ROUND.format(n=1))
        self.assertEqual(f.run("work", "1").returncode, 0)
        r = f.run("work", "done")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(status_of(tf), "done")

    def test_panel_required_all_rejects_single_judge_text(self):
        # Under the field's panel-always decree, single-judge TEXT evidence
        # ("impl review" in judge.md) must not satisfy the panel-grade bar.
        f = Fixture(config={"panel_required_for": "all"})
        tf = f.make_light_task("assertive", checked=True,
                               judge_md="# Impl Review\n\nsingle judge notes\n")
        self.assertEqual(f.run("work", "1").returncode, 0)
        r = f.run("work", "done")
        self.assertEqual(r.returncode, 1,
                         "panel-always accepted non-panel evidence:\n" + r.stdout)
        self.assertNotEqual(status_of(tf), "done")

    def test_reversible_light_closes_review_free(self):
        f = Fixture()
        tf = f.make_light_task("reversible", checked=True)
        self.assertEqual(f.run("work", "1").returncode, 0)
        r = f.run("work", "done")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(status_of(tf), "done")

    def test_unclassified_close_is_blocked_not_warned(self):
        """1.5.32: the template OFFERS `## Risk`, so leaving it unset is a
        skipped gate, not a legacy task — it is held to the high-consequence bar
        instead of closing on a warning. Before this, blank was the cheapest
        path through the strictest gate in the system."""
        f = Fixture()
        tf = f.make_light_task(None, checked=True)
        self.assertEqual(f.run("work", "1").returncode, 0)
        r = f.run("work", "done")
        self.assertEqual(r.returncode, 1,
                         "unset risk gate closed:\n" + r.stdout + r.stderr)
        self.assertIn("unclassified", (r.stdout + r.stderr).lower())
        self.assertNotEqual(status_of(tf), "done")

    def test_unclassified_close_passes_with_review_evidence(self):
        """Evidence is what the bar actually asks for — the same escape the
        assertive/irreversible classes have."""
        f = Fixture()
        tf = f.make_light_task(None, checked=True,
                               judge_md=PANEL_IMPL_ROUND.format(n=1))
        self.assertEqual(f.run("work", "1").returncode, 0)
        r = f.run("work", "done")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(status_of(tf), "done")
        # It still SAYS what could not be evaluated — silence was the original sin.
        self.assertIn("unclassified", (r.stdout + r.stderr).lower())

    def test_legacy_task_without_a_risk_section_still_closes_with_a_warning(self):
        """The pre-1.5.0 carve-out survives, narrowed to the tasks it was written
        for: no `## Risk` heading means the gate was never offered."""
        f = Fixture()
        tf = f.make_light_task(None, checked=True, strip_risk_section=True)
        self.assertEqual(f.run("work", "1").returncode, 0)
        r = f.run("work", "done")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(status_of(tf), "done")
        self.assertIn("unclassified", (r.stdout + r.stderr).lower())
        self.assertIn("⚠", r.stdout + r.stderr)

    def test_malformed_one_line_risk_is_treated_as_offered_and_blocks(self):
        # Judge Finding 3: "## Risk: assertive" (one line) is NOT a valid
        # heading — it degrades to unclassified. A BOTCHED gate is the same fact
        # as a skipped one, so it must land on the strict side rather than be
        # mistaken for a task that never had the section.
        f = Fixture()
        f.make_light_task(None, checked=True, malformed_risk_line=True)
        self.assertEqual(f.run("work", "1").returncode, 0)
        r = f.run("work", "done")
        self.assertEqual(r.returncode, 1,
                         "malformed risk heading closed:\n" + r.stdout + r.stderr)
        self.assertIn("unclassified", (r.stdout + r.stderr).lower())


class SwitchPathCannotClose(unittest.TestCase):
    """Judge Finding 1: `tasks work <N>` wrote `done` directly on a fully
    gated previous task — no risk, no verify, no receipt. The class fix:
    the switch path never closes; `tasks work done` is the only closer."""

    def test_switch_bounces_on_fully_gated_assertive_previous(self):
        f = Fixture()
        prev = f.make_light_task("assertive", checked=True)
        self.assertEqual(f.run("work", "1").returncode, 0)
        r2 = f.run("new", "light", "next thing")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        r = f.run("work", "2")
        self.assertEqual(r.returncode, 1,
                         "switch closed/skipped an unreviewed assertive task:\n"
                         + r.stdout + r.stderr)
        self.assertIn("tasks work done", r.stdout + r.stderr)
        self.assertNotEqual(status_of(prev), "done")

    def test_switch_bounces_on_reversible_previous_too(self):
        # The fix is the CLASS (no policy-free closes), not a risk special-case:
        # even a reversible fully-gated task must close through `work done`
        # (that is where the verify contract and the receipt live).
        f = Fixture()
        prev = f.make_light_task("reversible", checked=True)
        self.assertEqual(f.run("work", "1").returncode, 0)
        self.assertEqual(f.run("new", "light", "next thing").returncode, 0)
        r = f.run("work", "2")
        self.assertEqual(r.returncode, 1)
        self.assertNotEqual(status_of(prev), "done")

    def test_force_switches_away_leaving_task_open(self):
        f = Fixture()
        prev = f.make_light_task("assertive", checked=True)
        self.assertEqual(f.run("work", "1").returncode, 0)
        self.assertEqual(f.run("new", "light", "next thing").returncode, 0)
        r = f.run("work", "2", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(status_of(prev), "done",
                            "--force must leave the task honestly open, "
                            "never silently done")


class NewCommand(unittest.TestCase):
    def test_new_light_accepted_and_playbook_dump_suppressed(self):
        f = Fixture()
        r = f.run("new", "light", "tiny doc fix")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Created:", r.stdout)
        self.assertNotIn("=== PLAYBOOK", r.stdout,
                         "light must not dump the full playbook guide "
                         "(the ceremony it exists to shed)")

    def test_light_listed_in_types(self):
        f = Fixture()
        r = f.run("new")
        self.assertIn("light", r.stderr)


if __name__ == "__main__":
    unittest.main()
