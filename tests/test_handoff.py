#!/usr/bin/env python3
"""C1 — `tasks handoff`: codify the proven manual session-handoff pattern.

`tasks handoff` writes the mechanical ~80% of a handoff (project + nested-code-root
git state, gate progress, the latest verification receipt line, a timestamp) into
the ACTIVE task's `## Handoff` section, prints instructions for the agent to append
the judgment ~20%, and puts the task into the honest blocked state (reason
"handoff", reusing `tasks blocked` semantics — never a faked checkbox). A fresh
`tasks bootstrap` surfaces an unconsumed handoff prominently; resuming with
`tasks work <N>` consumes it (the section stays as history).

Run: python3 -m unittest tests.test_handoff
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"


def _git(d, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=d, check=True, capture_output=True)


def _env(session="pid-handoff"):
    return dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID=session)


def _cli(d, *args, session="pid-handoff"):
    return subprocess.run([sys.executable, "-m", "tasks.cli", *args],
                          cwd=d, env=_env(session), capture_output=True,
                          text=True, timeout=60)


class _Base(unittest.TestCase):
    def _project(self, *, code_root=False, receipt=True, git=True):
        d = Path(tempfile.mkdtemp())
        if git:
            subprocess.run(["git", "init", "-q"], cwd=d, check=True)
            (d / "code.py").write_text("x = 1\n", encoding="utf-8")
            _git(d, "add", "-A")
            _git(d, "commit", "-qm", "seed")
        (d / ".agent").mkdir(exist_ok=True)
        cfg = {}
        if code_root:
            (d / ".gitignore").write_text("sub/\n", encoding="utf-8")
            if git:
                _git(d, "add", "-A")
                _git(d, "commit", "-qm", "ignore sub")
            sub = d / "sub"
            sub.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=sub, check=True)
            (sub / "app.py").write_text("y = 1\n", encoding="utf-8")
            _git(sub, "add", "-A")
            _git(sub, "commit", "-qm", "seed nested")
            cfg["code_roots"] = ["sub"]
        if cfg:
            import json
            (d / ".agent" / "config.json").write_text(json.dumps(cfg),
                                                       encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-demo"
        td.mkdir(parents=True)
        receipt_block = ""
        if receipt:
            receipt_block = (
                "\n## Verification Receipt\n"
                "### 2026-08-25T10:00:00+00:00 · risk reversible · commit abc1234\n"
                "- **Commands:**\n    - [PASS] `python3 scripts/verify` (config)\n")
        (td / "task.md").write_text(
            "# 001 - Demo\n\n## Status\npending\n\n## Risk\nreversible\n\n"
            "## Work Plan\n- [x] G1: first thing\n- [ ] G2: the next thing\n"
            "- [ ] G3: after that\n" + receipt_block, encoding="utf-8")
        r = _cli(d, "work", "1")
        assert r.returncode == 0, r.stderr
        return d, td

    def _task_text(self, td):
        return (td / "task.md").read_text(encoding="utf-8")

    def _status(self, td):
        lines = self._task_text(td).splitlines()
        for i, ln in enumerate(lines):
            if ln.strip() == "## Status" and i + 1 < len(lines):
                return lines[i + 1].strip()
        return "?"


class HandoffSection(_Base):
    def test_writes_mechanical_section(self):
        d, td = self._project(code_root=True)
        r = _cli(d, "handoff")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self._task_text(td)
        self.assertIn("## Handoff", text)
        # project git state
        self.assertIn("Project repo", text)
        # a nested code_root line
        self.assertIn("sub", text)
        # gate progress + next unchecked (G2 is the first unchecked)
        self.assertIn("1/3", text)
        self.assertIn("G2: the next thing", text)
        # the latest verification receipt line
        self.assertIn("risk reversible", text)
        # a generated timestamp marker
        self.assertIn("Generated", text)
        # an agent-notes scaffold for the ~20%
        self.assertIn("Agent notes", text)

    def test_prints_agent_instructions(self):
        d, td = self._project()
        r = _cli(d, "handoff")
        out = r.stdout + r.stderr
        self.assertIn("append", out.lower())
        self.assertIn("Handoff", out)

    def test_blocks_with_reason_handoff_no_gate_touched(self):
        d, td = self._project()
        before = self._task_text(td)
        r = _cli(d, "handoff")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._status(td), "blocked")
        text = self._task_text(td)
        self.assertIn("## Blocked", text)
        self.assertIn("handoff", text.split("## Blocked", 1)[1][:120])
        # no gate flipped: same count of checked boxes
        self.assertEqual(before.count("- [x]"), text.count("- [x]"))
        self.assertEqual(before.count("- [ ]"), text.count("- [ ]"))

    def test_no_active_task_errors(self):
        d, td = self._project()
        _cli(d, "handoff")                       # blocks the task
        # deactivate by finishing the block is not needed; use a fresh session id
        r = _cli(d, "handoff", session="pid-none")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("active", (r.stdout + r.stderr).lower())

    def test_degrades_without_git_or_receipt(self):
        d, td = self._project(git=False, receipt=False)
        r = _cli(d, "handoff")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self._task_text(td)
        self.assertIn("## Handoff", text)          # still written
        self.assertEqual(self._status(td), "blocked")


class BootstrapSurfacing(_Base):
    def test_bootstrap_surfaces_unconsumed_handoff(self):
        d, td = self._project()
        _cli(d, "handoff")
        r = _cli(d, "bootstrap")
        out = r.stdout
        self.assertIn("handoff", out.lower())
        self.assertIn("001", out)                  # names the task
        self.assertIn("tasks work 1", out)         # resume hint

    def test_bootstrap_quiet_without_handoff(self):
        # Negative control: a project with an active (non-handoff) task must not
        # print any handoff-resume banner.
        d, td = self._project()
        r = _cli(d, "bootstrap")
        low = r.stdout.lower()
        self.assertNotIn("handoff waiting", low)
        self.assertNotIn("resume with tasks work", low)

    def test_resume_consumes_handoff_section_persists(self):
        d, td = self._project()
        _cli(d, "handoff")
        self.assertEqual(self._status(td), "blocked")
        r = _cli(d, "work", "1")                    # resume
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._status(td), "in_progress")
        # the ## Handoff section stays as history
        self.assertIn("## Handoff", self._task_text(td))
        # and bootstrap no longer surfaces it (consumed)
        r2 = _cli(d, "bootstrap")
        self.assertNotIn("resume with tasks work", r2.stdout.lower())


class FenceAndContainment(_Base):
    """Impl-panel hardening: fence-aware section writer/readers + code_root
    containment + git-failure not masked as clean."""

    def _core(self):
        sys.path.insert(0, str(PLUGIN))
        import tasks.core as core
        return core

    def test_write_handoff_ignores_fenced_heading(self):
        # Critical: a `## Handoff` quoted inside a fenced example must NOT be
        # treated as the section — writing must not delete through to the next
        # real H2 and corrupt the file.
        core = self._core()
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text(
            "# T\n\n## Notes\nExample of the section:\n```\n## Handoff\n"
            "old fenced content\n```\n\n## Keepers\n- [ ] must survive\n",
            encoding="utf-8")
        core.write_handoff(tf, "## Handoff\n> fresh\n- **Gates:** 0/1\n")
        text = tf.read_text(encoding="utf-8")
        self.assertIn("## Keepers", text)              # real H2 preserved
        self.assertIn("must survive", text)            # its content preserved
        self.assertIn("```", text)                     # the fence preserved
        self.assertIn("> fresh", text)                 # new section appended

    def test_repeated_handoff_preserves_prior_agent_notes(self):
        # R3/P3 (1.5.39): a handoff→resume→handoff sequence must NOT lose the
        # prior handoff's manually-appended Agent notes. The old block is
        # archived under `## Handoff history`, satisfying the docs' "stays behind
        # as history" claim for the multi-handoff case.
        import re
        core = self._core()
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text("# T\n\n## Work Plan\n- [ ] g\n", encoding="utf-8")
        core.write_handoff(tf, "## Handoff\n> Generated H1\n- **Gates:** 1/3\n\n"
                               "### Agent notes\n- In-flight reasoning:\n")
        # the agent fills a judgment note under Agent notes before stopping
        tf.write_text(
            tf.read_text(encoding="utf-8").replace(
                "- In-flight reasoning:",
                "- In-flight reasoning: FIRST-JUDGMENT-NOTE"),
            encoding="utf-8")
        # a fresh session resumes and later hands off again
        core.write_handoff(tf, "## Handoff\n> Generated H2\n- **Gates:** 2/3\n\n"
                               "### Agent notes\n- In-flight reasoning:\n")
        out = tf.read_text(encoding="utf-8")
        self.assertIn("FIRST-JUDGMENT-NOTE", out,
                      "repeated handoff deleted the prior handoff's Agent notes")
        self.assertIn("## Handoff history", out, "no history section created")
        self.assertIn("> Generated H2", out, "fresh handoff not written")
        # exactly ONE live `## Handoff` (the fresh one); the old is demoted to H3
        self.assertEqual(len(re.findall(r"(?m)^## Handoff$", out)), 1,
                         "there must be exactly one live ## Handoff section")
        # a THIRD handoff keeps BOTH prior handoffs' notes (history accumulates)
        tf.write_text(
            tf.read_text(encoding="utf-8").replace(
                "- In-flight reasoning:",
                "- In-flight reasoning: SECOND-JUDGMENT-NOTE"),
            encoding="utf-8")
        core.write_handoff(tf, "## Handoff\n> Generated H3\n- **Gates:** 3/3\n\n"
                               "### Agent notes\n- In-flight reasoning:\n")
        out3 = tf.read_text(encoding="utf-8")
        self.assertIn("FIRST-JUDGMENT-NOTE", out3)
        self.assertIn("SECOND-JUDGMENT-NOTE", out3)
        self.assertIn("> Generated H3", out3)
        self.assertEqual(len(re.findall(r"(?m)^## Handoff$", out3)), 1)
        self.assertEqual(out3.count("### Archived handoff"), 2,
                         "both prior handoffs must be archived")

    def test_block_reason_ignores_fenced_decoy(self):
        # A fenced `## Blocked` / `> handoff` example must not fake the reason;
        # the REAL block reason wins.
        core = self._core()
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text(
            "# T\n\n## Status\nblocked\n\n## Docs\n```\n## Blocked\n"
            "> handoff  (since x)\n```\n\n## Blocked\n> waiting on owner  (since y)\n",
            encoding="utf-8")
        self.assertEqual(core._extract_block_reason(tf), "waiting on owner")

    def test_find_unconsumed_handoff_skips_fenced_decoy(self):
        # A task blocked for a DIFFERENT reason, with a fenced handoff example,
        # must not be surfaced as an unconsumed handoff.
        core = self._core()
        d, td = self._project()
        (td / "task.md").write_text(
            "# 001\n\n## Status\nblocked\n\n## Docs\n```\n## Blocked\n"
            "> handoff\n```\n\n## Blocked\n> waiting  (since y)\n",
            encoding="utf-8")
        self.assertIsNone(core.find_unconsumed_handoff(d))

    def test_git_status_failure_is_status_unknown_not_clean(self):
        core = self._core()
        # dirty=None (status failed) must render "status unknown", not "clean".
        self.assertIn("status unknown",
                      core._repo_state_bullet("R", ("main", "abc1234", None)))
        self.assertIn("clean",
                      core._repo_state_bullet("R", ("main", "abc1234", 0)))

    def test_code_root_plain_subdir_is_not_ancestor_repo(self):
        # A code_root that is a plain subdir of the outer repo must report
        # "(not a git repo…)", not the ancestor repo's branch — require_own_toplevel.
        core = self._core()
        d, td = self._project()
        (d / "plaindir").mkdir()
        self.assertIsNone(
            core._git_repo_summary(d / "plaindir", require_own_toplevel=True))
        # the real nested repo IS its own toplevel
        d2, _ = self._project(code_root=True)
        self.assertIsNotNone(
            core._git_repo_summary(d2 / "sub", require_own_toplevel=True))


if __name__ == "__main__":
    unittest.main()
