#!/usr/bin/env python3
"""Task 036 — TAIL CERTIFICATION (owner decision A, ratified 2026-08-27).

After a quorum-PASS impl panel at tree F0, a close whose only post-panel delta
is in NON-BEHAVIORAL file-classes may satisfy panel freshness via a single-judge
tail certification instead of blocking / burning a fresh full panel. Any
code-path delta (file-class, incl. code comments) still requires a fresh panel.

This module holds the SAFETY-CRITICAL pure units and the close-path integration:

  * W1  classify_delta_paths        — file-class split (behavioral / non_behavioral)
  * W2  build_panel_snapshot        — the F0 descriptor embedded in the impl round
  * W3  tail_cert_delta             — the complete-superset delta enumeration
  * W4  tail_cert_gate_decision     — the pure allow/block policy
  * W5  close-path integration      — end-to-end through the real CLI

Every fail-closed rule is proven RED-first with a negative control.

Run: python3 -m unittest tests.test_tail_certification
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

from tasks.core import (  # noqa: E402
    build_panel_snapshot,
    classify_delta_paths,
    format_panel_snapshot_line,
    parse_judge_rounds,
    parse_tail_cert_verdict,
    tail_cert_delta,
    tail_cert_gate_decision,
    tree_state_fingerprint,
)


def _git(d, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=d, check=True, capture_output=True)


def _repo() -> Path:
    d = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "code.py").write_text("x = 1\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "seed")
    return d


class ClassifyDeltaPaths(unittest.TestCase):
    """W1 — the file-class table. Owner decision A + the H extension (ratified
    2026-08-27): non-behavioral = `*.md` under docs/ or basename
    README*/CHANGELOG*/MIND_MAP*; any path under a tests/ segment; any path under
    .agent/; PLUS (H) `*.json` under docs/ and the repo-root CLAUDE.md. Everything
    else is behavioral — file-class only, NEVER content (a code comment is still
    behavioral). The paths are SCOPE-RELATIVE (the enumerator classifies per scope
    before prefixing), so `CLAUDE.md` means a repo-root CLAUDE.md."""

    def _nb(self, path):
        beh, non = classify_delta_paths([path])
        self.assertEqual((beh, non), ([], [path]),
                         f"{path!r} should be NON-behavioral")

    def _beh(self, path):
        beh, non = classify_delta_paths([path])
        self.assertEqual((beh, non), ([path], []),
                         f"{path!r} should be BEHAVIORAL")

    # ---- non-behavioral ----
    def test_docs_md_is_nonbehavioral(self):
        self._nb("docs/architecture.md")
        self._nb("docs/nested/deep/guide.md")

    def test_docs_json_is_nonbehavioral(self):        # owner H extension
        self._nb("docs/guarantee-ledger.json")
        self._nb("docs/guarantee-ledger.baseline.json")

    def test_doc_basenames_anywhere(self):
        self._nb("README.md")
        self._nb("CHANGELOG.md")
        self._nb("MIND_MAP.md")
        self._nb("MIND_MAP_OVERFLOW.md")
        self._nb("sub/dir/README.md")

    def test_root_claude_md_is_nonbehavioral(self):   # owner H extension
        self._nb("CLAUDE.md")

    def test_tests_tree_is_nonbehavioral(self):
        self._nb("tests/test_foo.py")             # a test .py IS non-behavioral
        self._nb("plugins/playbook/tests/test_x.py")
        self._nb("tests/fixtures/data.txt")

    def test_agent_records_are_nonbehavioral(self):
        self._nb(".agent/tasks/036-t/task.md")
        self._nb(".agent/tasks/036-t/judge.md")

    # ---- behavioral (the safety-critical direction) ----
    def test_python_is_always_behavioral(self):
        # THE core safety property: a .py never lands in non_behavioral.
        self._beh("tasks/core.py")
        self._beh("scripts/verify")
        self._beh("plugins/playbook/tasks/review.py")

    def test_code_comment_only_file_class_still_behavioral(self):
        # File-class, not content: any .py is behavioral regardless of what
        # changed inside it (a comment-only edit still needs a fresh panel).
        self._beh("tasks/lifecycle.py")

    def test_toplevel_md_that_is_not_a_doc_name_is_behavioral(self):
        self._beh("NOTES.md")
        self._beh("design.md")

    def test_nested_claude_md_is_behavioral(self):    # only ROOT CLAUDE.md (H)
        self._beh("subproject/CLAUDE.md")

    def test_non_docs_json_is_behavioral(self):       # only docs/*.json (H)
        self._beh("config.json")
        self._beh("package.json")
        self._beh("plugins/playbook/hooks/hooks.json")

    def test_docs_txt_is_behavioral(self):            # only .md/.json under docs
        self._beh("docs/notes.txt")

    def test_unknown_extension_is_behavioral(self):
        self._beh("Makefile")
        self._beh("data.yaml")
        self._beh("x.sh")

    def test_readme_with_code_extension_is_behavioral(self):
        # Guard: the doc-basename rule is .md-only, so a README.py (contrived)
        # never sneaks code into non-behavioral.
        self._beh("README.py")

    # ---- shape ----
    def test_returns_sorted_deduped_split(self):
        beh, non = classify_delta_paths(
            ["tasks/core.py", "docs/a.md", "tasks/core.py", "README.md"])
        self.assertEqual(beh, ["tasks/core.py"])
        self.assertEqual(non, ["README.md", "docs/a.md"])

    def test_empty_and_blank_paths_ignored(self):
        self.assertEqual(classify_delta_paths(["", "   ", "."]), ([], []))


class PanelSnapshotDescriptor(unittest.TestCase):
    """W2 — the F0 descriptor embedded in the round (finding F). tree_fp is stored
    verbatim so descriptor/stamp are consistent by construction; per scope the
    HEAD commit + a content-token map of every dirty/untracked path is recorded,
    so the close can reconstruct the F0→final delta."""

    def test_tree_fp_stored_verbatim_and_outer_commit(self):
        d = _repo()
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        self.assertEqual(snap["tree_fp"], fp)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(snap["scopes"][""]["commit"], head)

    def test_dirty_and_untracked_paths_are_content_hashed(self):
        d = _repo()
        (d / "code.py").write_text("x = 2\n", encoding="utf-8")   # modify tracked
        (d / "new.txt").write_text("hello\n", encoding="utf-8")   # untracked
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))
        dirty = snap["scopes"][""]["dirty"]
        self.assertIn("code.py", dirty)
        self.assertIn("new.txt", dirty)
        self.assertTrue(dirty["code.py"].startswith("hash:"))
        self.assertTrue(dirty["new.txt"].startswith("hash:"))

    def test_agent_dir_excluded_from_descriptor(self):
        # `.agent/` never moves the fingerprint, so it must never appear in the
        # descriptor (owner decision A: task records excluded from the delta).
        d = _repo()
        (d / ".agent").mkdir()
        (d / ".agent" / "note.md").write_text("bookkeeping\n", encoding="utf-8")
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))
        self.assertEqual(snap["scopes"][""]["dirty"], {})

    def test_code_roots_scope_recorded(self):
        d = _repo()
        (d / ".gitignore").write_text("sub/\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "ignore sub")
        sub = d / "sub"
        sub.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=sub, check=True)
        (sub / "app.py").write_text("y = 1\n", encoding="utf-8")
        _git(sub, "add", "-A")
        _git(sub, "commit", "-qm", "seed nested")
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(
            json.dumps({"code_roots": ["sub"]}), encoding="utf-8")
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))
        self.assertIn("sub", snap["scopes"])
        self.assertTrue(snap["scopes"]["sub"]["commit"])

    def test_format_parse_round_trip(self):
        d = _repo()
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))
        line = format_panel_snapshot_line(snap)
        self.assertTrue(line.startswith("**Panel-snapshot:**"))
        self.assertNotIn("\n", line)   # single line — never breaks round parsing
        round_text = (
            "# Panel Impl Review — task 1\n\n"
            "**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
            f"**Tree-state:** {snap['tree_fp']}\n"
            f"{line}\n\nbody\n")
        rounds = parse_judge_rounds(round_text)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["snapshot"], snap)

    def test_malformed_snapshot_parses_to_none_not_crash(self):
        round_text = (
            "# Panel Impl Review — task 1\n\n"
            "**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
            "**Tree-state:** abcdef123456\n"
            "**Panel-snapshot:** {not valid json\n\nbody\n")
        rounds = parse_judge_rounds(round_text)
        self.assertEqual(rounds[0]["snapshot"], None)

    def test_round_without_snapshot_has_none(self):
        round_text = (
            "# Panel Impl Review — task 1\n\n"
            "**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
            "**Tree-state:** abcdef123456\n\nbody\n")
        rounds = parse_judge_rounds(round_text)
        self.assertEqual(rounds[0]["snapshot"], None)


class TailCertDelta(unittest.TestCase):
    """W3 — the SAFETY-CRITICAL enumeration. `tail_cert_delta(project, snapshot,
    round_tree_fp)` returns `(can_certify, behavioral, non_behavioral)`. Precondition
    (documented): it is only called when the tree is STALE vs the round stamp. The
    complete-superset proof: EVERY way the fingerprint could have moved since F0
    surfaces a path; fail-closed on every ambiguity. Both lists carry scope
    prefixes for display; classification is per-scope (scope-relative)."""

    def _snap_then(self, d, mutate, *, code_roots=None):
        """Build the F0 descriptor, then mutate the tree, then enumerate."""
        if code_roots is not None:
            (d / ".agent").mkdir(exist_ok=True)
            (d / ".agent" / "config.json").write_text(
                json.dumps({"code_roots": code_roots}), encoding="utf-8")
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        mutate(d)
        return tail_cert_delta(d, snap, snap["tree_fp"])

    # ---- complete-superset: every fingerprint-moving change surfaces ----
    def test_committed_since_panel_code_edit_surfaces(self):
        def mut(d):
            (d / "code.py").write_text("x = 99\n", encoding="utf-8")
            _git(d, "add", "-A")
            _git(d, "commit", "-qm", "post-panel commit")
        can, beh, non = self._snap_then(_repo(), mut)
        self.assertTrue(can)
        self.assertIn("code.py", beh)

    def test_uncommitted_code_edit_surfaces(self):
        can, beh, non = self._snap_then(
            _repo(), lambda d: (d / "code.py").write_text("x = 5\n", "utf-8"))
        self.assertTrue(can)
        self.assertIn("code.py", beh)

    def test_untracked_code_file_surfaces(self):
        can, beh, non = self._snap_then(
            _repo(), lambda d: (d / "new_mod.py").write_text("z = 1\n", "utf-8"))
        self.assertTrue(can)
        self.assertIn("new_mod.py", beh)

    def test_nested_code_root_edit_surfaces_with_prefix(self):
        d = _repo()
        (d / ".gitignore").write_text("sub/\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "ignore sub")
        sub = d / "sub"
        sub.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=sub, check=True)
        (sub / "app.py").write_text("y = 1\n", encoding="utf-8")
        _git(sub, "add", "-A")
        _git(sub, "commit", "-qm", "seed nested")
        can, beh, non = self._snap_then(
            d, lambda _d: (sub / "app.py").write_text("y = 42\n", "utf-8"),
            code_roots=["sub"])
        self.assertTrue(can)
        self.assertIn("sub/app.py", beh)   # scope-prefixed

    def test_dirty_at_panel_then_reverted_surfaces(self):
        # Finding B: a file dirty at F0 then reverted to HEAD has an EMPTY final
        # git diff, yet the fingerprint moved — the content-token comparison must
        # still surface it.
        d = _repo()
        (d / "code.py").write_text("x = 7\n", encoding="utf-8")   # dirty at F0
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        (d / "code.py").write_text("x = 1\n", encoding="utf-8")   # revert to HEAD
        can, beh, non = tail_cert_delta(d, snap, snap["tree_fp"])
        self.assertTrue(can)
        self.assertIn("code.py", beh)

    def test_rename_classifies_both_endpoints(self):
        # Finding A: `git mv code.py docs/x.md` collapses under --name-only to the
        # dest; --name-status -M must surface BOTH, so the deleted .py is caught
        # as behavioral (a production-code deletion never certifies as docs-only).
        d = _repo()
        (d / "docs").mkdir()
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        _git(d, "mv", "code.py", "docs/x.md")
        can, beh, non = tail_cert_delta(d, snap, snap["tree_fp"])
        self.assertTrue(can)
        self.assertIn("code.py", beh)          # the deleted source is behavioral
        self.assertIn("docs/x.md", non)        # the dest is a doc

    def test_docs_only_delta_certifiable(self):
        d = _repo()
        (d / "docs").mkdir()
        can, beh, non = self._snap_then(
            d, lambda _d: (d / "docs" / "guide.md").write_text("# hi\n", "utf-8"))
        self.assertTrue(can)
        self.assertEqual(beh, [])
        self.assertIn("docs/guide.md", non)

    # ---- fail-closed negative controls ----
    def test_missing_descriptor_fails_closed(self):
        d = _repo()
        self.assertEqual(tail_cert_delta(d, None, "abc123"), (False, [], []))

    def test_tree_fp_mismatch_fails_closed(self):
        d = _repo()
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))
        # the round stamp does NOT equal the descriptor's tree_fp
        self.assertEqual(tail_cert_delta(d, snap, "deadbeef0000"),
                         (False, [], []))

    def test_scope_set_change_fails_closed(self):
        # Finding C: a code_root added AFTER F0 makes the fingerprint stale while
        # the new root's code is absent from the descriptor — scope-set mismatch
        # must fail closed.
        d = _repo()
        (d / ".gitignore").write_text("sub/\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "ignore sub")
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))  # scopes = {""}
        sub = d / "sub"
        sub.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=sub, check=True)
        (sub / "app.py").write_text("y = 1\n", encoding="utf-8")
        _git(sub, "add", "-A")
        _git(sub, "commit", "-qm", "seed")
        (d / ".agent").mkdir(exist_ok=True)
        (d / ".agent" / "config.json").write_text(
            json.dumps({"code_roots": ["sub"]}), encoding="utf-8")
        self.assertEqual(tail_cert_delta(d, snap, snap["tree_fp"]),
                         (False, [], []))

    def test_empty_delta_while_stale_fails_closed(self):
        # Finding A: a stale tree with NO attributable path (e.g. a HEAD-sha-only
        # change) is unexplained → fail closed, never certify silently.
        d = _repo()
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))
        # no mutation at all → nothing attributable
        self.assertEqual(tail_cert_delta(d, snap, snap["tree_fp"]),
                         (False, [], []))


class TailCertGateDecision(unittest.TestCase):
    """W4 — the pure allow/block policy (no I/O). Reached only when the existing
    freshness gate would otherwise BLOCK (stale + carrying panel + high-consequence
    risk + no force/stale_ok)."""

    def test_not_certifiable_blocks(self):
        allowed, clause = tail_cert_gate_decision(
            can_certify=False, behavioral_nonempty=False,
            cert_verdict=None, non_behavioral=[])
        self.assertFalse(allowed)

    def test_behavioral_delta_blocks_needs_panel(self):
        allowed, clause = tail_cert_gate_decision(
            can_certify=True, behavioral_nonempty=True,
            cert_verdict="PASS", non_behavioral=["docs/x.md"])
        self.assertFalse(allowed)          # a code-path delta ALWAYS needs a panel
        self.assertIn("panel", clause.lower())

    def test_pass_certifies_with_receipt_clause(self):
        allowed, clause = tail_cert_gate_decision(
            can_certify=True, behavioral_nonempty=False,
            cert_verdict="PASS", non_behavioral=["docs/x.md", "README.md"])
        self.assertTrue(allowed)
        self.assertIn("docs/x.md", clause)
        self.assertIn("certif", clause.lower())

    def test_fail_verdict_blocks(self):
        allowed, clause = tail_cert_gate_decision(
            can_certify=True, behavioral_nonempty=False,
            cert_verdict="FAIL", non_behavioral=["docs/x.md"])
        self.assertFalse(allowed)

    def test_none_verdict_blocks(self):
        # A missing/unparseable judge verdict is fail-closed — never certifies.
        allowed, clause = tail_cert_gate_decision(
            can_certify=True, behavioral_nonempty=False,
            cert_verdict=None, non_behavioral=["docs/x.md"])
        self.assertFalse(allowed)


class TailCertVerdictParse(unittest.TestCase):
    """W6 — the structured-token parser. Fail-closed: only a SINGLE unambiguous
    `TAIL-CERT: PASS|FAIL` line on its own counts; anything else → None (block).
    Guards finding E: a prose grep for 'PASS' would false-certify."""

    def test_single_pass(self):
        self.assertEqual(
            parse_tail_cert_verdict("reasoning...\nTAIL-CERT: PASS\n"), "PASS")

    def test_single_fail(self):
        self.assertEqual(
            parse_tail_cert_verdict("nope\nTAIL-CERT: FAIL"), "FAIL")

    def test_missing_token_is_none(self):
        self.assertIsNone(parse_tail_cert_verdict("looks fine to me, PASS"))

    def test_empty_is_none(self):
        self.assertIsNone(parse_tail_cert_verdict(""))
        self.assertIsNone(parse_tail_cert_verdict(None))

    def test_non_terminal_pass_then_prose_is_none(self):
        # r5 grok#1/codex:sol#2: only the LAST non-empty line is the verdict, so a
        # PASS line buried in reasoning followed by prose does NOT certify.
        self.assertIsNone(
            parse_tail_cert_verdict("TAIL-CERT: PASS\nActually, on reflection...\n"))

    def test_non_terminal_pass_then_fail_last_line_is_fail(self):
        # A PASS earlier + a FAIL as the FINAL line = the judge's final word: FAIL.
        self.assertEqual(
            parse_tail_cert_verdict("TAIL-CERT: PASS\nTAIL-CERT: FAIL\n"), "FAIL")

    def test_prose_echo_of_instruction_is_none(self):
        # The judge echoing the instruction inline (not as the final line) → None,
        # never a spurious PASS.
        raw = "I was told to emit TAIL-CERT: PASS or TAIL-CERT: FAIL. Verdict:\n"
        self.assertIsNone(parse_tail_cert_verdict(raw))

    def test_error_string_is_none(self):
        # A spawn error ("(error: claude not found on PATH)") → None → block.
        self.assertIsNone(parse_tail_cert_verdict("(error: claude not found)"))

    def test_crlf_line_matches(self):
        # r2 grok#4: a Windows judge emitting CRLF must still certify.
        self.assertEqual(
            parse_tail_cert_verdict("ok\r\nTAIL-CERT: PASS\r\n"), "PASS")

    def test_nonce_required_when_given(self):
        # r2 grok#2: with a nonce, only the nonced token counts — unpredictable
        # doc content (a bare `TAIL-CERT: PASS`) can never forge a certification.
        n = "deadbeefcafe0001"
        self.assertEqual(
            parse_tail_cert_verdict(f"TAIL-CERT {n}: PASS\n", n), "PASS")
        self.assertIsNone(parse_tail_cert_verdict("TAIL-CERT: PASS\n", n))
        self.assertIsNone(parse_tail_cert_verdict("TAIL-CERT wrongnonce: PASS\n", n))


PLUGIN_STR = str(PLUGIN)


class ClosePathTailCert(unittest.TestCase):
    """W5 — the 6 check-questions end-to-end. Verdict-DEPENDENT cases inject the
    verdict IN-PROCESS by monkeypatching `run_tail_cert_judge` (impl-panel r3:
    there is NO ambient production judge seam — a gitignored config + env var was
    a real bypass, so it was removed). Cases that block BEFORE any judge spawn
    (behavioral / missing-descriptor / reversible / stale-ok) run as real
    subprocesses."""

    def _setup(self, *, risk="assertive", panel_cfg="all", stamp=True,
               snapshot=True):
        d = _repo()
        (d / "docs").mkdir()
        (d / ".agent").mkdir(exist_ok=True)
        if panel_cfg is not None:
            (d / ".agent" / "config.json").write_text(
                json.dumps({"panel_required_for": panel_cfg}), encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            f"# 001 - T\n\n## Status\npending\n\n## Risk\n{risk}\n\n"
            "## Work Plan\n- [x] G1: do it\n", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=PLUGIN_STR, PLAYBOOK_SESSION_ID="pid-036")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        # F0 state → the stamp + embedded descriptor
        fp = tree_state_fingerprint(d)
        stamp_line = f"**Tree-state:** {fp}\n" if stamp else ""
        snap_line = ""
        if snapshot:
            snap = build_panel_snapshot(d, fp)
            snap_line = format_panel_snapshot_line(snap) + "\n"
        (td / "judge.md").write_text(
            "# Panel Impl Review — task 1\n\n"
            "**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
            f"{stamp_line}{snap_line}\nbody\n", encoding="utf-8")
        return d, td, env

    def _close(self, d, env, *flags):
        """A real subprocess close (no judge verdict injected)."""
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", "work", "done", *flags],
            cwd=d, env=env, capture_output=True, text=True, timeout=90)

    def _close_inproc(self, d, verdict, *flags):
        """Close IN-PROCESS with the tail-cert judge patched to return `verdict`
        ("PASS"/"FAIL"/None) — the only safe way to drive the verdict path now that
        no production seam exists. Returns (stdout, stderr)."""
        import contextlib
        import io
        from unittest import mock
        from tasks.lifecycle import cmd_work
        out, err = io.StringIO(), io.StringIO()
        old_cwd = os.getcwd()
        old_env = dict(os.environ)
        os.chdir(d)
        os.environ["PLAYBOOK_SESSION_ID"] = "pid-036"
        os.environ["PYTHONPATH"] = PLUGIN_STR
        try:
            with mock.patch("tasks.review.run_tail_cert_judge",
                            return_value=verdict), \
                 contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(err):
                try:
                    cmd_work(["done", *flags])
                except SystemExit:
                    pass
        finally:
            os.chdir(old_cwd)
            os.environ.clear()
            os.environ.update(old_env)
        return out.getvalue(), err.getvalue()

    def _receipt(self, td):
        return (td / "task.md").read_text(encoding="utf-8")

    # (a) docs-only post-panel delta certifies WITHOUT --stale-panel-ok
    def test_docs_only_delta_certifies(self):
        d, td, env = self._setup()
        (d / "docs" / "guide.md").write_text("# new doc\n", encoding="utf-8")
        out, err = self._close_inproc(d, "PASS")
        self.assertIn("Task 001 done.", out, err)
        self.assertIn("tail-certified", err)
        self.assertIn("TAIL-CERTIFIED", self._receipt(td))

    # (b) a code (.py) delta blocks BEFORE the judge — even if it would PASS
    def test_code_delta_blocks_even_with_pass_stub(self):
        d, td, env = self._setup()
        (d / "code.py").write_text("x = 1  # comment\n", encoding="utf-8")
        out, err = self._close_inproc(d, "PASS")   # judge WOULD pass…
        self.assertNotIn("Task 001 done.", out)    # …but behavioral blocks first
        self.assertIn("pending", self._receipt(td))

    # (c) a FAIL certification blocks
    def test_fail_certification_blocks(self):
        d, td, env = self._setup()
        (d / "docs" / "guide.md").write_text("# new doc\n", encoding="utf-8")
        out, err = self._close_inproc(d, "FAIL")
        self.assertNotIn("Task 001 done.", out)
        self.assertIn("pending", self._receipt(td))

    # (c') a None verdict (unparseable/failed judge) → block
    def test_none_verdict_blocks(self):
        d, td, env = self._setup()
        (d / "docs" / "guide.md").write_text("# new doc\n", encoding="utf-8")
        out, err = self._close_inproc(d, None)
        self.assertNotIn("Task 001 done.", out)

    # (d) missing descriptor → fails closed even if the judge WOULD pass
    def test_missing_snapshot_falls_back_to_block(self):
        d, td, env = self._setup(snapshot=False)
        (d / "docs" / "guide.md").write_text("# new doc\n", encoding="utf-8")
        out, err = self._close_inproc(d, "PASS")
        self.assertNotIn("Task 001 done.", out)
        self.assertIn("code state changed after the newest impl panel", err)

    # (e) a reversible close still skips freshness entirely (advisory STALE)
    def test_reversible_still_advisory(self):
        d, td, env = self._setup(risk="reversible")
        (d / "docs" / "guide.md").write_text("# new doc\n", encoding="utf-8")
        r = self._close(d, env)   # freshness gate never fires — no judge
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))

    # (f) --stale-panel-ok --reason still closes (the escape is preserved)
    def test_stale_panel_ok_still_closes(self):
        d, td, env = self._setup()
        (d / "docs" / "guide.md").write_text("# new doc\n", encoding="utf-8")
        r = self._close(d, env, "--stale-panel-ok", "--reason", "docs tail reviewed")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))

    # a rename moving code into docs/ blocks (both endpoints classified) even if
    # the judge WOULD pass — the deleted .py is behavioral.
    def test_rename_code_to_doc_blocks(self):
        d, td, env = self._setup()
        r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            "mv", "code.py", "docs/moved.md"], cwd=d,
                           capture_output=True)
        assert r.returncode == 0
        out, err = self._close_inproc(d, "PASS")
        self.assertNotIn("Task 001 done.", out)   # deleted .py is behavioral


class Round2Fixes(unittest.TestCase):
    """Impl-panel round-2 fixes — each a fail-closed COMPLETENESS hole the panel
    found, pinned red-first with a negative control."""

    # I5 — a literal backslash in a POSIX filename is NOT a separator
    def test_backslash_filename_not_normalized(self):
        beh, non = classify_delta_paths(["tests\\evil.py"])
        self.assertEqual(beh, ["tests\\evil.py"])   # one behavioral segment
        self.assertEqual(non, [])
        # control: a real tests/ path IS non-behavioral
        self.assertEqual(classify_delta_paths(["tests/evil.py"]), ([], ["tests/evil.py"]))

    # I6 — the root-CLAUDE.md rule applies to the OUTER scope only
    def test_nested_scope_claude_md_is_behavioral(self):
        self.assertEqual(
            classify_delta_paths(["CLAUDE.md"], is_outer_scope=False),
            (["CLAUDE.md"], []))
        # control: outer scope root CLAUDE.md is non-behavioral
        self.assertEqual(
            classify_delta_paths(["CLAUDE.md"], is_outer_scope=True),
            ([], ["CLAUDE.md"]))

    # I4 — a mode change to an already-dirty code path surfaces (was content-only)
    def test_mode_change_to_dirty_code_surfaces(self):
        import os as _os
        import stat as _stat
        d = _repo()
        (d / "code.py").write_text("x = 2\n", encoding="utf-8")  # dirty at F0
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        # same content, mode change only (git tracks the exec bit)
        p = d / "code.py"
        _os.chmod(p, _os.stat(p).st_mode | _stat.S_IXUSR)
        can, beh, non = tail_cert_delta(d, snap, snap["tree_fp"])
        # if git records the mode change it must surface as behavioral; on a
        # filesystem/git that ignores the bit the tree is not stale — either way
        # NEVER a silent non-behavioral certification of code.
        if can or beh or non:
            self.assertIn("code.py", beh)

    # I3 — a git-error dirty map yields NO descriptor (not a clean {})
    def test_build_snapshot_none_on_no_head(self):
        # a fresh repo with no commits (unborn HEAD) → None, never a false-clean
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        self.assertIsNone(build_panel_snapshot(d, "abc123"))

    # I8 — an untracked certifiable file's CONTENT reaches the judge diff
    def test_review_diff_materializes_untracked_content(self):
        from tasks.review import _tail_cert_review_diff
        d = _repo()
        (d / "docs").mkdir()
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        (d / "docs" / "guide.md").write_text("CLAIM: 99% faster\n", encoding="utf-8")
        text = _tail_cert_review_diff(d, snap)
        self.assertIsNotNone(text)
        self.assertIn("CLAIM: 99% faster", text)   # the judge SEES the new claim
        self.assertIn("guide.md", text)

    # R3-3 — a trailing/leading space is a REAL byte, not stripped away
    def test_trailing_space_filename_is_behavioral(self):
        beh, non = classify_delta_paths(["CLAUDE.md "])   # note trailing space
        self.assertEqual(beh, ["CLAUDE.md "])
        self.assertEqual(non, [])
        # control: the exact root name is non-behavioral
        self.assertEqual(classify_delta_paths(["CLAUDE.md"]), ([], ["CLAUDE.md"]))

    # R3-2 — the STAGED (index) content is shown, not just the worktree
    def test_review_diff_shows_staged_index_content(self):
        from tasks.review import _tail_cert_review_diff
        d = _repo()
        (d / "docs").mkdir()
        (d / "docs" / "g.md").write_text("benign\n", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "seed doc")
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        # stage a FALSE claim, then revert the worktree to benign
        (d / "docs" / "g.md").write_text("FALSE CLAIM staged\n", encoding="utf-8")
        _git(d, "add", "docs/g.md")
        (d / "docs" / "g.md").write_text("benign\n", encoding="utf-8")
        text = _tail_cert_review_diff(d, snap)
        self.assertIsNotNone(text)
        self.assertIn("FALSE CLAIM staged", text)   # the shipping index is shown

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unsupported")
    def test_review_diff_refuses_to_follow_symlink(self):
        # r2 codex:sol#1/grok#1: an untracked docs symlink must NOT be followed
        # (no exfiltration), and a non-regular certifiable path fails closed.
        from tasks.review import _tail_cert_review_diff
        d = _repo()
        (d / "docs").mkdir()
        secret = Path(tempfile.mkdtemp()) / "secret"
        secret.write_text("TOP SECRET\n", encoding="utf-8")
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        os.symlink(secret, d / "docs" / "leak.md")
        text = _tail_cert_review_diff(d, snap)
        self.assertIsNone(text)                    # non-regular → fail closed
        if text:
            self.assertNotIn("TOP SECRET", text)

    # R5-2/grok#2/opus F2 — a large TRACKED doc edited by a little is shown as a
    # small DIFF, not its full content, so it stays under the transport limit.
    def test_large_tracked_doc_shown_as_small_diff(self):
        from tasks.review import _TAIL_CERT_TOTAL_CAP, _tail_cert_review_diff
        d = _repo()
        (d / "docs").mkdir()
        # a realistic large ledger: many lines (~300 KB), like docs/guarantee-ledger
        big = "".join(f'  {{"entry": {i}, "pad": "xxxxxxxxxxxxxxxx"}},\n'
                      for i in range(8000))
        (d / "docs" / "big.json").write_text('[\n' + big + ']\n', encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "big doc")
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        # a SMALL edit (append one line) to the large tracked file
        with open(d / "docs" / "big.json", "a", encoding="utf-8") as fh:
            fh.write('{"new":"claim"}\n')
        text = _tail_cert_review_diff(d, snap)
        self.assertIsNotNone(text)                  # certifiable, not fail-closed
        self.assertLess(len(text), _TAIL_CERT_TOTAL_CAP)   # small diff, not 300 KB
        self.assertIn("claim", text)                # the added claim is visible


class NoProductionSeam(unittest.TestCase):
    """r3 grok#2/codex:sol#4 — there is NO ambient production judge seam. A real
    subprocess close with an env var pointed at a would-be force-PASS command AND
    a `default_judge: __test_stub__` in the (gitignored) config still BLOCKS: the
    real adapter path runs, the unresolvable backend errors → None → block. Proves
    the round-2 bypass is gone."""

    def test_no_ambient_env_or_config_seam_can_force_pass(self):
        d, td, env = ClosePathTailCert("test_docs_only_delta_certifies")._setup()
        (d / ".agent" / "models.json").write_text(
            json.dumps({"default_judge": "__test_stub__"}), encoding="utf-8")
        (d / "docs" / "guide.md").write_text("# doc\n", encoding="utf-8")
        e = dict(env,
                 PLAYBOOK_TAIL_CERT_JUDGE_CMD="printf 'TAIL-CERT: PASS\\n'",
                 STUB_TAIL_VERDICT="PASS")
        r = subprocess.run(
            [sys.executable, "-m", "tasks.cli", "work", "done"],
            cwd=d, env=e, capture_output=True, text=True, timeout=90)
        self.assertNotIn("Task 001 done.", r.stdout)   # no seam → cannot force pass


class Round4Fixes(unittest.TestCase):
    """Impl-panel round-4 fixes, red-first with negative controls."""

    # R4-4 — `.agent` is non-behavioral only at the scope ROOT
    def test_nested_agent_dir_is_behavioral(self):
        self.assertEqual(classify_delta_paths(["src/.agent/runtime.py"]),
                         (["src/.agent/runtime.py"], []))
        # control: a ROOT .agent path stays non-behavioral
        self.assertEqual(classify_delta_paths([".agent/tasks/x/task.md"]),
                         ([], [".agent/tasks/x/task.md"]))

    # R4-3 — a post-panel change to fingerprint_exclude fails closed
    def test_exclude_set_change_fails_closed(self):
        d = _repo()
        (d / ".agent").mkdir()
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)                # exclude = default
        # add a fingerprint_exclude entry AFTER the panel, then touch code + docs
        (d / ".agent" / "config.json").write_text(
            json.dumps({"fingerprint_exclude": ["code.py"]}), encoding="utf-8")
        (d / "code.py").write_text("x = 99\n", encoding="utf-8")
        can, beh, non = tail_cert_delta(d, snap, snap["tree_fp"])
        self.assertFalse(can)                             # exclude-set changed → block
        self.assertEqual((beh, non), ([], []))

    # R4-1 — worktree-DELETED but index-STAGED: the staged blob is shown
    def test_review_diff_shows_staged_blob_when_worktree_deleted(self):
        from tasks.review import _tail_cert_review_diff
        d = _repo()
        (d / "docs").mkdir()
        fp = tree_state_fingerprint(d)
        snap = build_panel_snapshot(d, fp)
        # stage a false claim, then DELETE the worktree file (porcelain AD)
        (d / "docs" / "evil.md").write_text("FALSE staged claim\n", encoding="utf-8")
        _git(d, "add", "docs/evil.md")
        (d / "docs" / "evil.md").unlink()
        text = _tail_cert_review_diff(d, snap)
        self.assertIsNotNone(text)
        self.assertIn("FALSE staged claim", text)         # the shipping blob is shown
        self.assertNotIn("REMOVED/DELETED", text)         # NOT mislabeled withdrawn


class Round4Coverage(unittest.TestCase):
    """Exercise the guards the mocked close-path tests skip (impl-panel r4
    opus#1/#2)."""

    # opus#1 — the TOCTOU compare-and-swap actually blocks a mutate-during-cert
    def test_cas_blocks_when_judge_mutates_tree(self):
        d, td, env = ClosePathTailCert("test_docs_only_delta_certifies")._setup()
        (d / "docs" / "guide.md").write_text("# doc\n", encoding="utf-8")

        def _mutating_judge(*a, **k):
            (d / "code.py").write_text("x = 999\n", encoding="utf-8")  # perturb fp
            return "PASS"
        import contextlib
        import io
        from unittest import mock
        from tasks.lifecycle import cmd_work
        out, err = io.StringIO(), io.StringIO()
        old_cwd, old_env = os.getcwd(), dict(os.environ)
        os.chdir(d)
        os.environ["PLAYBOOK_SESSION_ID"] = "pid-036"
        os.environ["PYTHONPATH"] = PLUGIN_STR
        try:
            with mock.patch("tasks.review.run_tail_cert_judge",
                            side_effect=_mutating_judge), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    cmd_work(["done"])
                except SystemExit:
                    pass
        finally:
            os.chdir(old_cwd)
            os.environ.clear()
            os.environ.update(old_env)
        self.assertNotIn("Task 001 done.", out.getvalue())
        self.assertIn("changed during certification", err.getvalue())


class RunTailCertJudgeGuards(unittest.TestCase):
    """opus#2 — run_tail_cert_judge's own fail-closed guards (unmocked)."""

    def test_failed_judge_tail_with_pass_token_is_none(self):
        from unittest import mock

        import tasks.review as R
        d = _repo()
        (d / "docs").mkdir()
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))
        (d / "docs" / "g.md").write_text("# d\n", encoding="utf-8")
        # a crashed judge whose stdout tail happens to contain a nonced PASS
        def _raw(project_path, prompt, timeout_secs):
            import re
            m = re.search(r"TAIL-CERT ([0-9a-f]+):", prompt)
            n = m.group(1) if m else "x"
            return f"(FAILED — exit 1)\n[stdout tail]\nTAIL-CERT {n}: PASS\n"
        with mock.patch.object(R, "_run_tail_cert_judge_raw", side_effect=_raw):
            v = R.run_tail_cert_judge(d, snap, ["docs/g.md"], "PANEL PASS")
        self.assertIsNone(v)                    # crashed judge never certifies

    def test_error_string_is_none(self):
        from unittest import mock

        import tasks.review as R
        d = _repo()
        (d / "docs").mkdir()
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))
        (d / "docs" / "g.md").write_text("# d\n", encoding="utf-8")
        with mock.patch.object(R, "_run_tail_cert_judge_raw",
                               return_value="(error: judge not found)"):
            self.assertIsNone(
                R.run_tail_cert_judge(d, snap, ["docs/g.md"], "PANEL PASS"))


if __name__ == "__main__":
    unittest.main()
