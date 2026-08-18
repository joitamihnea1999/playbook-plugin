"""Red-first tests for the 1.5.13 panel-acceptance fix batch.

Each test reproduces a CONFIRMED, hand-reproduced finding from the five-model
final-acceptance panel (reviews/round1-*.md, round2-*.md; adjudicated in
verification-report-1.5.12-panel.md). Every test here FAILS on 1.5.12 and passes
after the fix.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "playbook"


def _run(args, cwd, extra_env=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PLUGIN)
    env["PLAYBOOK_SESSION_ID"] = "pid-test"
    env.pop("BASH_ENV", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                          cwd=str(cwd), env=env, text=True, capture_output=True)


class _Proj(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.p = Path(self._tmp.name)
        (self.p / ".agent" / "tasks").mkdir(parents=True)


class F11WorkSlugPointer(_Proj):
    def test_work_with_NNN_slug_writes_numeric_pointer(self):
        """`tasks work 001-fix-widget` must leave a NUMERIC session pointer the
        numeric-only gate accepts — not the raw slug that strands the agent."""
        _run(["new", "light", "fix-widget", "x"], self.p)
        r = _run(["work", "001-fix-widget"], self.p)
        self.assertEqual(r.returncode, 0, r.stderr)
        ptr = (self.p / ".agent/sessions/pid-test/current_state").read_text().strip()
        self.assertEqual(ptr, "001",
                         f"pointer should be canonical numeric, got {ptr!r}")

    def test_work_with_bare_slug_writes_numeric_pointer(self):
        _run(["new", "light", "fix-widget", "x"], self.p)
        r = _run(["work", "fix-widget"], self.p)
        self.assertEqual(r.returncode, 0, r.stderr)
        ptr = (self.p / ".agent/sessions/pid-test/current_state").read_text().strip()
        self.assertEqual(ptr, "001", f"got {ptr!r}")


class F6LightStubIntent(_Proj):
    def test_stub_light_intent_survives_expansion(self):
        _run(["new", "--stub", "light", "demo", "KEEP_THIS_INTENT"], self.p)
        _run(["work", "1"], self.p)
        txt = (self.p / ".agent/tasks/001-demo/task.md").read_text()
        self.assertIn("KEEP_THIS_INTENT", txt,
                      "light stub intent dropped on activation (B1 twin)")


class F7F18CustomStubExpansion(_Proj):
    def _mk_playbook(self, name):
        pb = self.p / ".agent" / "playbooks"
        pb.mkdir(parents=True, exist_ok=True)
        (pb / f"{name}.md").write_text(
            "# {{NNN}} - {{TITLE}}\n\n## Status\npending\n\n## Intent\n"
            "(custom)\n\n- [ ] ONLY_CUSTOM_GATE\n", encoding="utf-8")

    def test_alnum_custom_stub_expands_to_its_own_playbook(self):
        """F18: a custom stub must expand to its custom playbook, not the base
        Build template (which silently drops the custom gates)."""
        self._mk_playbook("speval")
        _run(["new", "--stub", "speval", "demo"], self.p)
        _run(["work", "1"], self.p)
        txt = (self.p / ".agent/tasks/001-demo/task.md").read_text()
        self.assertIn("ONLY_CUSTOM_GATE", txt,
                      "custom stub expanded to the wrong template — custom gate lost")

    def test_hyphenated_custom_stub_expands(self):
        """F7: the stub marker regex must accept hyphenated custom type names."""
        self._mk_playbook("sp-eval")
        _run(["new", "--stub", "sp-eval", "demo"], self.p)
        _run(["work", "1"], self.p)
        txt = (self.p / ".agent/tasks/001-demo/task.md").read_text()
        self.assertNotIn("stub:sp-eval", txt,
                         "hyphenated stub marker never expanded (regex \\w+ excludes '-')")
        self.assertIn("ONLY_CUSTOM_GATE", txt)


class F8CustomPlaybookIntent(_Proj):
    """1.5.17: the `[intent]` arg reaches a custom playbook via `{{INTENT}}`."""

    def _mk_playbook(self, body):
        pb = self.p / ".agent" / "playbooks"
        pb.mkdir(parents=True, exist_ok=True)
        (pb / "speval.md").write_text(body, encoding="utf-8")

    def test_intent_token_is_filled(self):
        self._mk_playbook("# {{NNN}} - {{TITLE}}\n\n## Status\npending\n\n"
                          "## Intent\n{{INTENT}}\n\n- [ ] G\n")
        _run(["new", "speval", "demo", "SHIP_THE_EXPORT"], self.p)
        txt = (self.p / ".agent/tasks/001-demo/task.md").read_text()
        self.assertIn("SHIP_THE_EXPORT", txt, "intent did not reach the custom playbook")
        self.assertNotIn("{{INTENT}}", txt, "token left unsubstituted")

    def test_no_intent_arg_clears_the_token(self):
        self._mk_playbook("# {{NNN}} - {{TITLE}}\n\n## Status\npending\n\n"
                          "## Intent\n{{INTENT}}\n\n- [ ] G\n")
        _run(["new", "speval", "demo"], self.p)
        txt = (self.p / ".agent/tasks/001-demo/task.md").read_text()
        self.assertNotIn("{{INTENT}}", txt, "token left unsubstituted when no intent given")

    def test_playbook_without_token_is_unchanged(self):
        # Backwards-compatible: a custom playbook that never opts into {{INTENT}}
        # is untouched (NNN/TITLE still stamped).
        self._mk_playbook("# {{NNN}} - {{TITLE}}\n\n## Status\npending\n\n- [ ] G\n")
        _run(["new", "speval", "demo", "SOME_INTENT"], self.p)
        txt = (self.p / ".agent/tasks/001-demo/task.md").read_text()
        self.assertIn("001 - Demo", txt)


class F5PanelRequiredCaseFold(_Proj):
    def test_case_variant_all_still_requires_panel(self):
        """A near-miss like "ALL" must not silently disable the seeded panel gate."""
        sys.path.insert(0, str(PLUGIN))
        from tasks.core import resolve_panel_required  # noqa
        (self.p / ".agent" / "config.json").write_text(
            json.dumps({"panel_required_for": "ALL"}), encoding="utf-8")
        self.assertTrue(resolve_panel_required(self.p, "reversible"),
                        '"ALL" should resolve as "all" (case-fold), not fail open')


class F13IntentFallback(unittest.TestCase):
    def test_intent_default_fallback_is_not_codex(self):
        """intent.py's unset-config fallback must match the all-Claude default
        (review.py falls back to claude), not codex."""
        src = (PLUGIN / "tasks" / "intent.py").read_text(encoding="utf-8")
        self.assertNotIn('or "codex"', src,
                         "intent.py still falls back to codex when default_judge unset")


class DocReconcile(unittest.TestCase):
    def test_gate_hook_type_list_has_no_invalid_types(self):
        """F17: task-gate-hook remediation text must not advertise rejected types."""
        src = (PLUGIN / "scripts" / "task-gate-hook").read_text(encoding="utf-8")
        for bad in ("explore, research, review, decision, test",):
            self.assertNotIn(bad, src, f"stale type list still present: {bad}")

    def test_review_usage_not_ships_codex(self):
        """F12: default judge is opus; usage must not claim it 'ships codex'."""
        src = (PLUGIN / "tasks" / "review.py").read_text(encoding="utf-8")
        self.assertNotIn("ships codex", src)

    def test_upgrade_md_uses_playbook_init(self):
        """F14: upgrade doc must point at /playbook:init, not bare /init."""
        src = (PLUGIN / "commands" / "upgrade.md").read_text(encoding="utf-8")
        self.assertIn("/playbook:init", src)
        # No bare `/init` as an instruction (the built-in generic generator).
        self.assertNotIn("Run `/init`", src)
        self.assertNotIn("Run /init", src)

    def test_docs_skill_bundle_count_matches_reality(self):
        """F15: the docs' claimed count of discoverable SKILL.md bundles must
        equal how many actually ship — a stale number (either direction) is the
        bug. Checked generically so adding a skill can't silently drift the docs."""
        skill_dirs = sorted(d.name for d in (PLUGIN / "skills").iterdir()
                            if (d / "SKILL.md").exists())
        words = {5: "Five", 6: "Six", 7: "Seven", 8: "Eight"}
        word = words.get(len(skill_dirs))
        self.assertIsNotNone(word, f"add a count word for {len(skill_dirs)} skills")
        cli = (REPO_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
        arch = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        self.assertIn(f"{word} skill bundles", cli,
                      f"docs/cli.md must say '{word} skill bundles' ({skill_dirs})")
        self.assertIn(f"{word.lower()} harness-discoverable skill bundles", arch,
                      f"docs/architecture.md count is stale ({skill_dirs})")
        # And every shipped skill must be named in the cli.md list.
        for name in skill_dirs:
            self.assertIn(name, cli, f"docs/cli.md never names the '{name}' skill")


if __name__ == "__main__":
    unittest.main()
