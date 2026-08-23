#!/usr/bin/env python3
"""`init` writes CLAUDE.md + .gitignore mechanically, merging never clobbering (F15).

Gauntlet F15: those two files were the AGENT half of /playbook:init — the
doctrine held only if the agent performed it (it did on the real project;
the fragility is the finding). The mechanical write guarantees it.

The hard requirement from batch-1: init on a project with a SEEDED CLAUDE.md
(an owner's pointer paragraph above everything) must MERGE — pointer preserved,
template sections added/updated — never clobber. That shape was verified live
in the field and is the negative control here, plus: template-owned sections
refresh on re-init while custom sections and preambles survive byte-for-byte,
runs are idempotent, and .gitignore appends its marker-guarded block exactly
once without touching existing content.

Covers the module (unit) and the real `scripts/init` run (fixture, isolated
HOME so ~/.claude is never mutated — the S14 lesson).

Run: python3 -m unittest tests.test_init_claude_md
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from tests._bashcheck import bash_or_skip
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
SCRIPTS = PLUGIN / "scripts"

_spec = importlib.util.spec_from_file_location(
    "claude_md_merge", SCRIPTS / "claude-md-merge.py")
cmm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cmm)

TEMPLATE = (SCRIPTS / "CLAUDE.md.template").read_text(encoding="utf-8")

SEEDED = """# StrataDB

**Read `PURPOSE.md` first** — this project's role and your journal duty.

## Dev Tooling

pyright is installed; stdlib-only at runtime.
"""


class MergeClaudeMd(unittest.TestCase):
    def test_fresh_write_substitutes_name_and_strips_header(self):
        out = cmm.merge_claude_md(TEMPLATE, None, "My Proj")
        self.assertTrue(out.startswith("# My Proj\n"), out[:40])
        self.assertNotIn("PLAYBOOK TEMPLATE", out)
        self.assertIn("## Correctness Contract", out)

    def test_seeded_pointer_survives_and_sections_append(self):
        out = cmm.merge_claude_md(TEMPLATE, SEEDED, "StrataDB")
        # The owner's seed stays at the very top, byte-for-byte.
        self.assertTrue(out.startswith("# StrataDB\n"))
        self.assertIn("**Read `PURPOSE.md` first**", out)
        self.assertLess(out.index("PURPOSE.md"), out.index("## Start Here"))
        # Custom section preserved; template sections all present.
        self.assertIn("## Dev Tooling\n\npyright is installed", out)
        for h in ("## Start Here", "## Task Lifecycle", "## Correctness Contract",
                  "## CLI", "## Don't"):
            self.assertIn(h, out)

    def test_template_owned_section_updates_in_place(self):
        stale = ("# P\n\nkeep me\n\n## Correctness Contract\n\nOLD 1.5.0 WORDING\n\n"
                 "## My Rules\n\nnever bulk-surgery source\n")
        out = cmm.merge_claude_md(TEMPLATE, stale, "P")
        self.assertNotIn("OLD 1.5.0 WORDING", out)
        self.assertIn("the WORLD reverts", out)  # current template body landed
        self.assertIn("## My Rules\n\nnever bulk-surgery source", out)
        # Updated in place: contract stays BEFORE the custom section.
        self.assertLess(out.index("## Correctness Contract"), out.index("## My Rules"))

    def test_idempotent(self):
        once = cmm.merge_claude_md(TEMPLATE, SEEDED, "StrataDB")
        twice = cmm.merge_claude_md(TEMPLATE, once, "StrataDB")
        self.assertEqual(once, twice)

    def test_clobber_is_impossible_for_unrelated_content(self):
        # Negative control: a CLAUDE.md with NO template sections at all comes
        # through with every original byte still present.
        original = "# Legacy\n\nhand-written rules the owner cares about\n\n## Ops\n\nrestart with systemctl\n"
        out = cmm.merge_claude_md(TEMPLATE, original, "Legacy")
        self.assertIn("hand-written rules the owner cares about", out)
        self.assertIn("## Ops\n\nrestart with systemctl", out)


class MergeGitignore(unittest.TestCase):
    def test_created_when_absent(self):
        out = cmm.merge_gitignore(None)
        self.assertIn(cmm.GITIGNORE_MARKER, out)
        self.assertIn(".agent/sessions/", out)
        self.assertIn(".agent/*/sessions/", out)  # multi-user lanes covered
        self.assertIn(".agent/models.json", out)
        # T2: the enforcement journal is machine-local runtime state (the block
        # predated it), both root and per-user lanes.
        self.assertIn(".agent/journal/", out)
        self.assertIn(".agent/*/journal/", out)

    def test_appends_once_preserving_content(self):
        existing = "# mine\n__pycache__/\n"
        out = cmm.merge_gitignore(existing)
        self.assertTrue(out.startswith("# mine\n__pycache__/\n"))
        self.assertIn(cmm.GITIGNORE_MARKER, out)
        self.assertIsNone(cmm.merge_gitignore(out), "second run must be a no-op")


class InitWritesBothFiles(unittest.TestCase):
    """The real scripts/init run — the seam the gauntlet found fragile."""

    def _run_init(self, proj: Path) -> str:
        home = Path(tempfile.mkdtemp()) / "home"
        home.mkdir(parents=True)
        env = dict(os.environ, HOME=str(home))
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        r = subprocess.run([bash_or_skip(), str(SCRIPTS / "init"), "Fixture Proj"],
                           cwd=proj, env=env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def _project(self) -> Path:
        proj = Path(tempfile.mkdtemp()) / "proj"
        proj.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
        return proj

    def test_bare_repo_gets_both_files(self):
        proj = self._project()
        self._run_init(proj)
        claude = proj / "CLAUDE.md"
        self.assertTrue(claude.exists(), "init did not write CLAUDE.md")
        text = claude.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Fixture Proj"))
        self.assertIn("## Correctness Contract", text)
        gi = proj / ".gitignore"
        self.assertTrue(gi.exists(), "init did not write .gitignore")
        self.assertIn(".agent/sessions/", gi.read_text(encoding="utf-8"))

    def test_seeded_pointer_survives_reinit(self):
        # THE negative control: merge, never clobber.
        proj = self._project()
        (proj / "CLAUDE.md").write_text(SEEDED, encoding="utf-8")
        (proj / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        self._run_init(proj)
        text = (proj / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("**Read `PURPOSE.md` first**", text)
        self.assertTrue(text.startswith("# StrataDB"))
        self.assertIn("## Dev Tooling", text)
        self.assertIn("## Correctness Contract", text)
        gi = (proj / ".gitignore").read_text(encoding="utf-8")
        self.assertTrue(gi.startswith("__pycache__/"))
        self.assertIn(".agent/sessions/", gi)
        # Re-init: idempotent, still no clobber.
        before = text
        self._run_init(proj)
        self.assertEqual((proj / "CLAUDE.md").read_text(encoding="utf-8"), before)
        self.assertEqual((proj / ".gitignore").read_text(encoding="utf-8"), gi)


if __name__ == "__main__":
    unittest.main()
