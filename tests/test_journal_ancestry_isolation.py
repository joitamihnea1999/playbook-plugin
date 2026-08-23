#!/usr/bin/env python3
"""Regression + class guard for the dogfooded-ancestry ENFORCEMENT-JOURNAL leak.

The live defect: the unittest suite is run from inside a playbook-managed
workspace (the dogfooded dev repo). `test_command_guard`'s guard subprocess is
launched with NO `cwd=`, so it inherits the test-runner's cwd; `command_guard`'s
`_find_root()` then walks UP to the real workspace `.agent/tasks`, and the block
for the test vector "rm -rf /" is appended to the owner's REAL
`.agent/journal/enforcement.jsonl`. Observed live: 18 `command-guard` /
`rm-rf-dangerous-target` records with an empty `session_id`.

The owning boundary is the test itself: a journal-emitting hook run in a
subprocess must be given an isolated `cwd=` with no `.agent` ancestor, exactly as
`test_enforcement_journal` runs everything under a temp project. This module
pins that contract with a harness that simulates the ancestry independently of
any real machine layout (mirrors `test_intent_ancestry_isolation`):

  * `JournalAncestryHarnessIsArmed` — the negative control: prove that an
    UNISOLATED guard subprocess DOES leak into the simulated ancestor, and an
    isolated one does NOT. Reintroduce a `cwd`-less emitter invocation and this
    certifies the trap below is real, not a dead fixture.
  * `GuardTestHelperIsAncestrySafe` — the red-first regression: run the REAL
    `test_command_guard.HookBehavior._run` helper under the simulated ancestor
    and assert the ancestor journal gained NOTHING. Before the fix (the helper
    had no `cwd=`) this FAILS with a leaked `command-guard` record.

Pure stdlib unittest (stdlib-only runtime invariant). The simulated ancestor is
a temp dir, so a regression here can never write a real machine's journal.

Run: python3 tests/test_journal_ancestry_isolation.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                       # sibling test modules
PLUGIN = _HERE.parent / "plugins/playbook"
GUARD = PLUGIN / "scripts" / "command_guard.py"

DANGEROUS = '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}'


@contextmanager
def dogfooded_journal_ancestry():
    """Yield (ancestor, checkout): a checkout dir BENEATH a freshly-minted
    playbook-managed `.agent/` (config + a `tasks/` dir, so `_find_root` treats
    the ancestor as the project root). `cwd` is moved into the checkout for the
    body, so any hook that resolves `.agent` by walking up from cwd finds THIS
    temp ancestor — never the tester's real machine layout. Restored on exit."""
    saved_cwd = os.getcwd()
    tmp = tempfile.TemporaryDirectory(prefix="dogfood-journal-ancestor-")
    try:
        ancestor = Path(tmp.name)
        agent = ancestor / ".agent"
        (agent / "tasks" / "001-real").mkdir(parents=True)
        (agent / "tasks" / "001-real" / "task.md").write_text(
            "# 001\n## Status\npending\n", encoding="utf-8")
        (agent / "config.json").write_text("{}", encoding="utf-8")
        checkout = ancestor / "checkout"
        checkout.mkdir()
        os.chdir(checkout)
        yield ancestor, checkout
    finally:
        os.chdir(saved_cwd)
        tmp.cleanup()


def _journal_records(ancestor: Path, hook: str = "command-guard") -> "list[dict]":
    p = ancestor / ".agent" / "journal" / "enforcement.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("hook") == hook:
            out.append(rec)
    return out


def _run_guard(payload: str, *, cwd) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.pop("PLAYBOOK_ALLOW_DANGEROUS", None)
    kwargs = {}
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    return subprocess.run(["python3", str(GUARD)], input=payload,
                          capture_output=True, text=True, env=e, **kwargs)


class JournalAncestryHarnessIsArmed(unittest.TestCase):
    """Negative control: prove the trap a leak trips is real."""

    def test_unisolated_guard_leaks_but_isolated_does_not(self):
        with dogfooded_journal_ancestry() as (ancestor, _checkout):
            # UNISOLATED: no cwd → inherits the checkout cwd → _find_root walks
            # up to the ancestor .agent → the block is journalled THERE.
            r = _run_guard(DANGEROUS, cwd=None)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertTrue(
                _journal_records(ancestor),
                "harness is not armed: an unisolated guard run should have "
                "leaked a command-guard record into the ancestor journal",
            )

        with dogfooded_journal_ancestry() as (ancestor, _checkout):
            # ISOLATED: cwd is a temp dir with no .agent ancestor → _find_root
            # returns None → nothing is journalled.
            with tempfile.TemporaryDirectory() as iso:
                r = _run_guard(DANGEROUS, cwd=iso)
            self.assertEqual(r.returncode, 2, r.stderr)  # still BLOCKS
            self.assertEqual(
                _journal_records(ancestor), [],
                "an isolated (cwd without an .agent ancestor) guard run must "
                "not write any journal",
            )


class GuardTestHelperIsAncestrySafe(unittest.TestCase):
    """Red-first regression at the owning boundary: the REAL command-guard test
    helper must not leak into an ancestor journal. Before the fix the helper ran
    the guard with no `cwd=`, so this failed with a leaked record."""

    def _make_helper(self):
        """A real `test_command_guard.HookBehavior` instance WITH its fixture —
        `setUp` is where the cwd-isolation lives, so exercise the helper exactly
        as the framework runs it (setUp → call → doCleanups)."""
        import test_command_guard as tcg
        hb = tcg.HookBehavior("test_blocks_dangerous_bash_payload")
        hb.setUp()
        self.addCleanup(hb.doCleanups)
        return hb

    def test_command_guard_helper_does_not_leak(self):
        with dogfooded_journal_ancestry() as (ancestor, _checkout):
            hb = self._make_helper()
            r = hb._run(DANGEROUS)
            self.assertEqual(r.returncode, 2, r.stderr)   # still BLOCKS
            self.assertEqual(
                _journal_records(ancestor), [],
                "test_command_guard.HookBehavior._run leaked a command-guard "
                "record into the ancestor journal — it must run the guard in an "
                "isolated cwd with no .agent ancestor",
            )

    def test_command_guard_wrapper_helper_does_not_leak(self):
        with dogfooded_journal_ancestry() as (ancestor, _checkout):
            hb = self._make_helper()
            r = hb._run_hook(
                '{"toolName":"Shell","toolInput":{"command":"rm -rf /"},'
                '"hookEventName":"PreToolUse"}')
            self.assertEqual(r.returncode, 2, r.stderr)   # still BLOCKS
            self.assertEqual(
                _journal_records(ancestor), [],
                "test_command_guard.HookBehavior._run_hook leaked into the "
                "ancestor journal — the wrapper run needs an isolated cwd too",
            )


if __name__ == "__main__":
    unittest.main()
