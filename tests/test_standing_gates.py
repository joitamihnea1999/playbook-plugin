#!/usr/bin/env python3
"""Standing gates (F8): project-declared gates always appended LAST to every task.

Field evidence (StrataDB batches 2/2b): the project's journal gate had to be
hand-relocated below Pre-review VERBATIM on two consecutive tasks — "that it
recurred verbatim is itself the signal." Projects want to declare gates
(journal, changelog…) that generation appends as the final gates of every
task, instead of relying on the agent to re-add them by hand each time.

Contract pinned here:

  * `standing_gates` in `.agent/config.json` — a list of {title, text};
  * appended as the FINAL sections (one `## <title>` + one `- [ ] <text>`
    gate each, declared order) of every generated task: base template, quick,
    custom playbook append, and stub EXPANSION (stubs have no gates until
    activated);
  * `{{NNN}}` in title/text substitutes the zero-padded task number (the
    journal use case: `journal/{{NNN}}.md`);
  * opt-in: no config → output byte-identical to before (negative control);
  * malformed entries and title collisions with existing headings are SKIPPED
    LOUDLY, and embedded newlines cannot mint phantom sections or gates (the
    #09 multi-heading disease — negative controls).

Run: python3 -m unittest tests.test_standing_gates
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

from tasks.core import append_standing_gates, create_task  # noqa: E402
from tasks.template import render_template  # noqa: E402

JOURNAL = {"title": "Journal", "text": "Write journal/{{NNN}}.md — Shipped / Friction / Value / Honesty-check / Ignored / One-change"}
CHANGELOG = {"title": "Changelog", "text": "Add the user-visible change to CHANGELOG.md"}
CFG = {"standing_gates": [JOURNAL, CHANGELOG]}


def _project(cfg: "dict | None") -> Path:
    proj = Path(tempfile.mkdtemp())
    (proj / ".agent" / "tasks").mkdir(parents=True)
    if cfg is not None:
        (proj / ".agent" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return proj


class AppendHelper(unittest.TestCase):
    def test_appends_in_declared_order_at_the_end(self):
        content = "# 001 - T\n\n## Status\npending\n\n## Pre-review\n- [ ] tests\n"
        out, issues = append_standing_gates(content, CFG, 1)
        self.assertEqual(issues, [])
        self.assertTrue(out.index("## Journal") < out.index("## Changelog"))
        # Final gates: nothing but the declared sections after Pre-review's gate.
        tail = out[out.index("## Journal"):]
        self.assertEqual(
            [l for l in tail.splitlines() if l.startswith("- [ ]")],
            ["- [ ] Write journal/001.md — Shipped / Friction / Value / Honesty-check / Ignored / One-change",
             "- [ ] Add the user-visible change to CHANGELOG.md"])

    def test_nnn_substitution_zero_pads(self):
        out, _ = append_standing_gates("# 042 - T\n", CFG, 42)
        self.assertIn("journal/042.md", out)

    def test_no_config_is_byte_identical(self):
        content = render_template(1, "control", "feature")
        out, issues = append_standing_gates(content, {}, 1)
        self.assertEqual(out, content)
        self.assertEqual(issues, [])

    def test_malformed_entries_skipped_loudly(self):
        for bad_cfg in (
            {"standing_gates": "not-a-list"},
            {"standing_gates": [{"title": "NoText"}]},
            {"standing_gates": [{"text": "no title"}]},
            {"standing_gates": [{"title": "", "text": "empty title"}]},
            {"standing_gates": ["just a string"]},
        ):
            with self.subTest(cfg=bad_cfg):
                content = "# 001 - T\n"
                out, issues = append_standing_gates(content, bad_cfg, 1)
                self.assertEqual(out, content, "malformed entry changed output")
                self.assertTrue(issues, "malformed entry was skipped SILENTLY")

    def test_title_collision_with_existing_heading_skipped_loudly(self):
        content = "# 001 - T\n\n## Pre-review\n- [ ] tests\n"
        cfg = {"standing_gates": [{"title": "Pre-review", "text": "x"}, JOURNAL]}
        out, issues = append_standing_gates(content, cfg, 1)
        self.assertEqual(out.count("## Pre-review"), 1,
                         "a colliding title minted a duplicate heading (#09)")
        self.assertTrue(any("Pre-review" in i for i in issues))
        self.assertIn("## Journal", out)  # the valid one still lands

    def test_embedded_newlines_cannot_mint_phantom_gates(self):
        # The parsers are line-anchored (1.5.0), so the invariant is about
        # line STARTS: a config value must not be able to open a new heading
        # line or a new gate line (#09 disease via config).
        cfg = {"standing_gates": [
            {"title": "Sneaky\n## Fake Section", "text": "line one\n- [ ] phantom gate\n## Another"}]}
        out, _ = append_standing_gates("# 001 - T\n", cfg, 1)
        lines = out.splitlines()
        self.assertFalse(
            [l for l in lines if l.startswith("## Fake Section") or l.startswith("## Another")],
            "an embedded newline minted a phantom section")
        gates = [l for l in lines if l.startswith("- [ ]")]
        self.assertEqual(len(gates), 1, f"phantom gate minted: {gates}")


class GenerationEndsWithStandingGates(unittest.TestCase):
    def test_base_template_task_ends_with_them_in_order(self):
        proj = _project(CFG)
        tf = create_task(proj, "build thing", "feature")
        text = tf.read_text(encoding="utf-8")
        self.assertIn("## Journal\n- [ ] Write journal/001.md", text)
        j, c = text.index("## Journal"), text.index("## Changelog")
        self.assertLess(j, c)
        self.assertGreater(j, text.index("## Pre-review"))
        # LAST gates in the file, in order.
        gates = [l for l in text.splitlines() if l.startswith("- [ ]")]
        self.assertTrue(gates[-1].startswith("- [ ] Add the user-visible change"))
        self.assertTrue(gates[-2].startswith("- [ ] Write journal/001.md"))

    def test_quick_task_gets_them_too(self):
        proj = _project(CFG)
        tf = create_task(proj, "small fix", "quick")
        text = tf.read_text(encoding="utf-8")
        gates = [l for l in text.splitlines() if l.startswith("- [ ]")]
        self.assertTrue(gates[-1].startswith("- [ ] Add the user-visible change"))

    def test_custom_playbook_content_stays_above_standing_gates(self):
        proj = _project(CFG)
        pb = proj / ".agent" / "playbooks"
        pb.mkdir(parents=True)
        (pb / "feature.md").write_text(
            "# {{NNN}} - {{TITLE}}\n\n## Status\npending\n\n## Work Plan\n- [ ] custom gate\n",
            encoding="utf-8")
        tf = create_task(proj, "custom thing", "feature")
        text = tf.read_text(encoding="utf-8")
        self.assertLess(text.index("- [ ] custom gate"), text.index("## Journal"))
        gates = [l for l in text.splitlines() if l.startswith("- [ ]")]
        self.assertTrue(gates[-1].startswith("- [ ] Add the user-visible change"))

    def test_without_config_output_unchanged(self):
        # The negative control: opt-in means the render is IDENTICAL when the
        # key is absent — no seeded standing gates, no placeholder section.
        tf = create_task(_project(None), "control task", "feature")
        text = tf.read_text(encoding="utf-8")
        self.assertNotIn("## Journal", text)
        self.assertNotIn("## Changelog", text)
        self.assertEqual(text, render_template(1, "Control Task", "feature"),
                         "no-config output drifted from the plain template")


class StubExpansionAppliesStandingGates(unittest.TestCase):
    def test_stub_gains_standing_gates_on_activation(self):
        proj = _project(CFG)
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-f8")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "new", "--stub",
                            "feature", "stub thing"],
                           cwd=proj, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        tf = next((proj / ".agent" / "tasks").glob("001-*/task.md"))
        self.assertNotIn("## Journal", tf.read_text(encoding="utf-8"),
                         "a stub has no gates until activation")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=proj, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = tf.read_text(encoding="utf-8")
        gates = [l for l in text.splitlines() if l.startswith("- [ ]")]
        self.assertTrue(gates, "stub did not expand")
        self.assertTrue(gates[-1].startswith("- [ ] Add the user-visible change"),
                        f"standing gates missing/misplaced after expansion: {gates[-3:]}")
        self.assertIn("- [ ] Write journal/001.md", text)


if __name__ == "__main__":
    unittest.main()
