#!/usr/bin/env python3
"""F18 — irreversible closes resolve panel staleness explicitly (blind-judge
reviewed design: design-1.5.6.md + judge-f18-design.md, all findings built).

Three layers:

  * `tree_state_fingerprint` coverage (judge C1: the old fingerprint was
    PROVABLY blind to edits in untracked files — where batches 4 and 5 put
    their post-panel fixes) + config-declared excludes (judge C2: standing-
    gate outputs like journal/NNN.md must be excludable or the gate blocks
    100% of well-behaved closes on the flagship project);
  * the pure gate decision (`freshness_gate_decision`);
  * the real CLI close path: block matrix, narrow override, receipt clauses
    (judge F4: a missing stamp is RECORDED, not silently skipped; judge F5:
    a shared --reason under --force attributes to force).

Run: python3 -m unittest tests.test_panel_freshness_gate
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
    _repo_fingerprint_material,
    freshness_gate_decision,
    tree_state_fingerprint,
)

BLOCK_MARKER = "code state changed after the newest impl panel"
CLAUSE = "**Panel tree-state:**"


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


class FingerprintCoverage(unittest.TestCase):
    def test_untracked_file_edit_moves_the_fingerprint(self):
        # Judge C1, reproduced red against the 1.5.3 fingerprint: porcelain
        # names an untracked file but not its content; diff HEAD skips it.
        d = _repo()
        (d / "new_module.py").write_text("VERSION = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        (d / "new_module.py").write_text("VERSION = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2,
                            "fingerprint is blind to untracked-file edits — "
                            "the batch-4/5 post-panel fix shape")

    def test_untracked_file_inside_new_directory_covered(self):
        # porcelain without -uall shows only `?? dir/` — the file inside is
        # invisible unless untracked enumeration is per-file.
        d = _repo()
        (d / "pkg").mkdir()
        (d / "pkg" / "mod.py").write_text("a = 1\n", encoding="utf-8")
        fp1 = tree_state_fingerprint(d)
        (d / "pkg" / "mod.py").write_text("a = 2\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp2)

    def test_agent_dir_still_excluded(self):
        # Negative control: the original exclusion must survive the rewrite —
        # triage edits between panel and close are the designed flow.
        d = _repo()
        (d / ".agent" / "tasks" / "001-t").mkdir(parents=True)
        fp1 = tree_state_fingerprint(d)
        (d / ".agent" / "tasks" / "001-t" / "task.md").write_text("- [x] g\n",
                                                                  encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertEqual(fp1, fp2)

    def test_config_declared_exclude_is_honored(self):
        # Judge C2: owner-declared bookkeeping (StrataDB journal/) must be
        # excludable, or the irreversible gate fires on every close.
        d = _repo()
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(
            json.dumps({"fingerprint_exclude": ["journal/"]}), encoding="utf-8")
        (d / "journal").mkdir()
        fp1 = tree_state_fingerprint(d)
        (d / "journal" / "011.md").write_text("shipped\n", encoding="utf-8")
        fp2 = tree_state_fingerprint(d)
        self.assertEqual(fp1, fp2, "declared exclude did not suppress journal noise")
        # and a real code edit still moves it (exclusion is narrow)
        (d / "code.py").write_text("x = 3\n", encoding="utf-8")
        self.assertNotEqual(fp2, tree_state_fingerprint(d))

    def test_malformed_exclude_skipped_loudly_not_silently(self):
        d = _repo()
        (d / ".agent").mkdir()
        (d / ".agent" / "config.json").write_text(
            json.dumps({"fingerprint_exclude": ["ok/", 7, ""]}), encoding="utf-8")
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fp = tree_state_fingerprint(d)
        self.assertTrue(fp, "malformed entries must not kill the fingerprint")
        self.assertIn("fingerprint_exclude", buf.getvalue(),
                      "malformed entries must be skipped LOUDLY")


class NestedCodeRoots(unittest.TestCase):
    """C2: the outer fingerprint is blind to a code-only edit inside a nested
    (gitignored) checkout — the real-world blind spot (this workspace and
    HowFar-v2 keep their code in a gitignored nested repo). `code_roots` folds
    each nested repo's HEAD+porcelain+diff+untracked into the fingerprint."""

    def _outer_with_nested(self):
        """Outer repo with a gitignored nested git repo at `sub/`. Editing files
        inside `sub/` must not move the OUTER porcelain/diff/untracked at all —
        which is exactly why the outer fingerprint is blind to it."""
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
        (d / ".agent").mkdir(exist_ok=True)
        return d, sub

    def _cfg(self, d, obj):
        (d / ".agent").mkdir(exist_ok=True)
        (d / ".agent" / "config.json").write_text(json.dumps(obj), encoding="utf-8")

    def test_nested_edit_moves_fingerprint_when_registered(self):
        # THE core fix. code_roots names `sub`; a code-only edit inside `sub`
        # (tracked file, and separately an untracked one) must move the outer fp.
        d, sub = self._outer_with_nested()
        self._cfg(d, {"code_roots": ["sub"]})
        fp1 = tree_state_fingerprint(d)
        self.assertTrue(fp1)
        (sub / "app.py").write_text("y = 2\n", encoding="utf-8")  # dirty tracked
        self.assertNotEqual(fp1, tree_state_fingerprint(d),
                            "registered nested edit did not move the fingerprint")
        # a NEW commit in the nested repo (HEAD moves) also registers
        _git(sub, "add", "-A")
        _git(sub, "commit", "-qm", "bump")
        fp_committed = tree_state_fingerprint(d)
        self.assertNotEqual(fp1, fp_committed)
        # and an untracked file inside the nested repo, by content
        (sub / "extra.py").write_text("Z = 1\n", encoding="utf-8")
        fp_unt1 = tree_state_fingerprint(d)
        (sub / "extra.py").write_text("Z = 2\n", encoding="utf-8")
        self.assertNotEqual(fp_unt1, tree_state_fingerprint(d),
                            "untracked nested content is not hashed")

    def test_unset_code_roots_is_byte_identical(self):
        # Unset (and empty-list) must be EXACTLY today's behavior: a nested edit
        # leaves the outer fingerprint unchanged, and unset == [] byte-for-byte.
        d, sub = self._outer_with_nested()
        # no config at all
        fp_no_cfg = tree_state_fingerprint(d)
        (sub / "app.py").write_text("y = 999\n", encoding="utf-8")
        self.assertEqual(fp_no_cfg, tree_state_fingerprint(d),
                         "with code_roots unset a nested edit must be invisible "
                         "(today's behavior) — the byte-identical guarantee")
        # empty list is identical to unset
        (sub / "app.py").write_text("y = 1\n", encoding="utf-8")  # restore
        self._cfg(d, {"code_roots": []})
        self.assertEqual(fp_no_cfg, tree_state_fingerprint(d),
                         "code_roots: [] must equal unset")

    def test_missing_or_non_repo_root_is_deterministic_not_crash(self):
        # A configured root that does not exist / is not a git repo must not
        # crash the fingerprint and must be deterministic across calls.
        d, sub = self._outer_with_nested()
        self._cfg(d, {"code_roots": ["does-not-exist"]})
        fp1 = tree_state_fingerprint(d)
        self.assertTrue(fp1)
        self.assertEqual(fp1, tree_state_fingerprint(d))
        # a plain (non-git) directory root
        (d / "plaindir").mkdir()
        self._cfg(d, {"code_roots": ["plaindir"]})
        fp2 = tree_state_fingerprint(d)
        self.assertTrue(fp2)
        self.assertEqual(fp2, tree_state_fingerprint(d))

    def test_invalid_entries_skipped_loudly(self):
        # Absolute paths, `..` traversal, and non-strings are rejected LOUDLY;
        # the fingerprint is still produced from the valid remainder.
        import contextlib
        import io
        d, sub = self._outer_with_nested()
        self._cfg(d, {"code_roots": ["sub", "/etc", "../escape", 7, ""]})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fp = tree_state_fingerprint(d)
        self.assertTrue(fp, "invalid entries must not kill the fingerprint")
        err = buf.getvalue()
        self.assertIn("code_roots", err, "invalid entries must be skipped LOUDLY")
        # the valid `sub` still took effect
        (sub / "app.py").write_text("y = 7\n", encoding="utf-8")
        self.assertNotEqual(fp, tree_state_fingerprint(d))

    def test_unset_matches_legacy_oracle(self):
        # Impl-panel F1: pin that the OUTER computation is byte-identical to the
        # pre-feature formula — not just unset==[]. An INDEPENDENT reimplementation
        # of the legacy head+porcelain+diff+untracked algorithm must equal the
        # live unset fingerprint, so any concatenation reorder in
        # _repo_fingerprint_material turns this red (it would otherwise pass the
        # whole suite while silently shifting every stamp → one-time mass-STALE).
        import hashlib
        d = _repo()
        (d / "code.py").write_text("x = 5\n", encoding="utf-8")   # dirty tracked
        (d / "unt...tracked.py").write_text("NEW = 1\n", encoding="utf-8")  # untracked
        _git(d, "add", "code.py")
        _git(d, "commit", "-qm", "second")
        (d / "code.py").write_text("x = 6\n", encoding="utf-8")   # dirty again

        exclude = [":(exclude).agent"]
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                              capture_output=True, text=True).stdout.strip()
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "--", ".", *exclude],
            cwd=d, capture_output=True, text=True).stdout
        diff = subprocess.run(["git", "diff", "HEAD", "--", ".", *exclude],
                              cwd=d, capture_output=True, text=True).stdout
        ud = hashlib.sha256()
        for line in sorted(porcelain.splitlines()):
            if not line.startswith("?? "):
                continue
            rel = line[3:].strip().strip('"')
            fhash = hashlib.sha256((d / rel).read_bytes()).hexdigest()
            ud.update(f"{rel}\0{fhash}\n".encode("utf-8", "replace"))
        oracle = hashlib.sha256(
            (head + porcelain + diff + ud.hexdigest()).encode("utf-8", "replace")
        ).hexdigest()[:12]
        self.assertEqual(oracle, tree_state_fingerprint(d),
                         "outer fingerprint drifted from the legacy formula — "
                         "byte-identical guarantee broken")

    def test_strict_requires_own_repo_toplevel(self):
        # Impl-panel F2: a plain subdirectory of the outer repo is NOT its own
        # repo. strict mode must return None (→ <absent>), not silently
        # fingerprint the ANCESTOR repo git walks up to.
        d, sub = self._outer_with_nested()
        (d / "plaindir").mkdir()
        (d / "plaindir" / "f.py").write_text("q = 1\n", encoding="utf-8")
        exclude = [":(exclude).agent"]
        self.assertIsNone(
            _repo_fingerprint_material(d / "plaindir", exclude, strict=True),
            "a plain subdir must be <absent>, not the ancestor repo")
        # the real nested repo IS its own toplevel → real material
        self.assertIsNotNone(
            _repo_fingerprint_material(sub, exclude, strict=True))
        # and via the full fingerprint, a plain-subdir root does not adopt the
        # outer repo's identity under a nested label
        self._cfg(d, {"code_roots": ["plaindir"]})
        # deterministic + truthy (folds an <absent> marker, not ancestor bytes)
        self.assertTrue(tree_state_fingerprint(d))
        self.assertEqual(tree_state_fingerprint(d), tree_state_fingerprint(d))

    @unittest.skipUnless(hasattr(os, "symlink"), "requires os.symlink")
    def test_symlink_root_escaping_the_tree_is_skipped(self):
        # Impl-panel F2: a code_roots entry that is lexically clean ("link", no
        # `..`, relative) but SYMLINKS to a git repo OUTSIDE the project must be
        # refused loudly — never fingerprint outside the tree.
        import contextlib
        import io
        outside = Path(tempfile.mkdtemp())              # a repo outside the tree
        subprocess.run(["git", "init", "-q"], cwd=outside, check=True)
        (outside / "secret.py").write_text("s = 1\n", encoding="utf-8")
        _git(outside, "add", "-A")
        _git(outside, "commit", "-qm", "outside")
        d, sub = self._outer_with_nested()
        try:
            os.symlink(outside, d / "link")
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted here")
        self._cfg(d, {"code_roots": ["link"]})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fp_escape = tree_state_fingerprint(d)
        self.assertIn("resolves outside the project", buf.getvalue(),
                      "symlink escape must be skipped LOUDLY")
        # editing the OUTSIDE repo must not move our fingerprint at all
        (outside / "secret.py").write_text("s = 2\n", encoding="utf-8")
        self.assertEqual(fp_escape, tree_state_fingerprint(d),
                         "fingerprint was steered to hash outside the tree")


class GateDecision(unittest.TestCase):
    KW = dict(risk="irreversible", panel_required=True, evidence_carries=True,
              round_fp="a" * 12, now_fp="b" * 12, force=False,
              stale_ok=False, stale_reason=None)

    def test_stale_irreversible_blocks(self):
        allowed, why = freshness_gate_decision(**self.KW)
        self.assertFalse(allowed)
        self.assertIn("re-run", why)
        self.assertIn("--stale-panel-ok", why)

    def test_override_without_reason_refused(self):
        allowed, why = freshness_gate_decision(**{**self.KW, "stale_ok": True})
        self.assertFalse(allowed)
        self.assertIn("--reason", why)

    def test_override_with_reason_allows(self):
        allowed, _ = freshness_gate_decision(
            **{**self.KW, "stale_ok": True, "stale_reason": "journal only"})
        self.assertTrue(allowed)

    def test_gate_scope_negative_controls(self):
        # Each condition individually off → allowed (the gate is narrow).
        # NOTE (T1, 2026-08-23): {"risk": "assertive"} and — after panel finding
        # O1 — {"risk": "unclassified"} MOVED OUT of this allowed set. Both now
        # block on a stale carrying panel (see test_stale_assertive_blocks /
        # test_stale_unclassified_blocks); only `reversible` stays advisory.
        # Rewriting, not deleting, the old contract.
        for tweak in ({"risk": "reversible"},
                      {"panel_required": False}, {"evidence_carries": False},
                      {"round_fp": ""}, {"now_fp": ""},
                      {"now_fp": "a" * 12}, {"force": True}):
            allowed, _ = freshness_gate_decision(**{**self.KW, **tweak})
            self.assertTrue(allowed, f"gate overreached with {tweak}")

    def test_stale_assertive_blocks(self):
        # T1: an assertive close resting on a stale panel must block, because a
        # claim signed off by a panel that predates the code is a claim about
        # code that was never reviewed. Same message shape as irreversible.
        allowed, why = freshness_gate_decision(
            **{**self.KW, "risk": "assertive"})
        self.assertFalse(allowed, "assertive stale close was NOT blocked")
        self.assertIn("re-run", why)
        self.assertIn("--stale-panel-ok", why)
        # grok#3: pin the message CONTENT — the risk class and BOTH fingerprints
        # (worklist: "blocks the close with a message naming both fingerprints").
        # A revert to the old hardcoded "risk is irreversible" or a dropped
        # fingerprint must fail here, not stay green on the marker alone.
        self.assertIn("assertive", why)
        self.assertIn(self.KW["round_fp"], why)
        self.assertIn(self.KW["now_fp"], why)

    def test_assertive_override_without_reason_refused(self):
        allowed, why = freshness_gate_decision(
            **{**self.KW, "risk": "assertive", "stale_ok": True})
        self.assertFalse(allowed)
        self.assertIn("--reason", why)

    def test_assertive_override_with_reason_allows(self):
        # Narrow escape (same as irreversible): --stale-panel-ok --reason.
        allowed, _ = freshness_gate_decision(
            **{**self.KW, "risk": "assertive", "stale_ok": True,
               "stale_reason": "docs-only delta, diff reviewed"})
        self.assertTrue(allowed)

    def test_assertive_force_allows(self):
        # Blunt escape (same as irreversible): --force bypasses close policy.
        allowed, _ = freshness_gate_decision(
            **{**self.KW, "risk": "assertive", "force": True})
        self.assertTrue(allowed)

    def test_stale_unclassified_blocks(self):
        # Panel finding O1: an unset `## Risk` is held to the high-consequence
        # bar everywhere else (close_decision, panel_required "all"), so it must
        # ALSO block on a stale carrying panel — otherwise blanking the field is
        # strictly more lenient on freshness than honest classification, the
        # 1.5.32 "cheapest path through the strictest gate" fail-open reopened.
        allowed, why = freshness_gate_decision(
            **{**self.KW, "risk": "unclassified"})
        self.assertFalse(allowed, "unclassified stale close was NOT blocked")
        self.assertIn("re-run", why)


class ClosePathMatrix(unittest.TestCase):
    """End-to-end through the real CLI."""

    def _setup(self, *, risk: str, panel_cfg, round_head: str = "Impl",
               verdict: str = "PASS", stamp: bool = True,
               change_after: bool = True, extra_round: "str | None" = None,
               code_roots=None, nested_change: bool = False):
        d = _repo()
        (d / ".agent").mkdir(exist_ok=True)
        sub = None
        if code_roots is not None:
            # gitignored nested git repo(s) — the code_roots dogfood shape.
            (d / ".gitignore").write_text(
                "".join(f"{r}/\n" for r in code_roots), encoding="utf-8")
            _git(d, "add", "-A")
            _git(d, "commit", "-qm", "ignore nested roots")
            for r in code_roots:
                sub = d / r
                sub.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "init", "-q"], cwd=sub, check=True)
                (sub / "app.py").write_text("y = 1\n", encoding="utf-8")
                _git(sub, "add", "-A")
                _git(sub, "commit", "-qm", "seed nested")
        cfg = {}
        if panel_cfg is not None:
            cfg["panel_required_for"] = panel_cfg
        if code_roots is not None:
            cfg["code_roots"] = code_roots
        if cfg:
            (d / ".agent" / "config.json").write_text(
                json.dumps(cfg), encoding="utf-8")
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            f"# 001 - T\n\n## Status\npending\n\n## Risk\n{risk}\n\n"
            "## Work Plan\n- [x] G1: do it\n", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(PLUGIN), PLAYBOOK_SESSION_ID="pid-f18")
        r = subprocess.run([sys.executable, "-m", "tasks.cli", "work", "1"],
                           cwd=d, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        fp = tree_state_fingerprint(d)
        stamp_line = f"**Tree-state:** {fp}\n" if stamp else ""
        rounds = (f"# Panel {round_head} Review — task 1\n\n"
                  f"**PANEL VERDICT: {verdict}** — 4/4, quorum 3\n"
                  f"{stamp_line}\nbody\n")
        if extra_round:
            rounds = extra_round + "\n" + rounds
        (td / "judge.md").write_text(rounds, encoding="utf-8")
        if change_after:
            if nested_change:
                # A CODE-ONLY edit INSIDE the nested root — nothing in the outer
                # tree moves. Only a code_roots-aware fingerprint catches it.
                assert sub is not None, "nested_change requires code_roots"
                (sub / "app.py").write_text("y = 42\n", encoding="utf-8")
            else:
                (d / "code.py").write_text("x = 99\n", encoding="utf-8")
        return d, td, env

    def _close(self, d, env, *flags):
        return subprocess.run(
            [sys.executable, "-m", "tasks.cli", "work", "done", *flags],
            cwd=d, env=env, capture_output=True, text=True, timeout=60)

    def _receipt(self, td) -> str:
        return (td / "task.md").read_text(encoding="utf-8")

    def test_irreversible_stale_blocks(self):
        d, td, env = self._setup(risk="irreversible", panel_cfg="all")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        self.assertIn("pending", self._receipt(td))

    def test_override_needs_reason_then_closes_with_recorded_reason(self):
        d, td, env = self._setup(risk="irreversible", panel_cfg="all")
        r = self._close(d, env, "--stale-panel-ok")
        self.assertNotIn("Task 001 done.", r.stdout)
        r = self._close(d, env, "--stale-panel-ok", "--reason", "journal only, diff reviewed")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn(CLAUSE, receipt)
        self.assertIn("STALE", receipt)
        self.assertIn('accepted: "journal only, diff reviewed"', receipt)

    def test_irreversible_fresh_closes_with_fresh_clause(self):
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 change_after=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("FRESH", self._receipt(td))

    def test_reversible_stale_closes_with_stale_clause(self):
        # Advisory behavior unchanged below the gate; the record is new.
        # (T1: reversible stays advisory — the negative control that the gate
        # did not widen to every risk class.)
        d, td, env = self._setup(risk="reversible", panel_cfg="all")
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))
        self.assertIn("tree-state mismatch", r.stdout)  # console note kept

    def test_assertive_stale_blocks(self):
        # T1: the real close path blocks an assertive stale close, same as
        # irreversible. Red against pre-T1 code (assertive closed with a STALE
        # receipt clause and no block).
        d, td, env = self._setup(risk="assertive", panel_cfg="all")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        self.assertIn("risk is assertive", r.stderr)  # grok#3: names the class
        self.assertIn("pending", self._receipt(td))

    def test_assertive_fresh_closes_with_fresh_clause(self):
        d, td, env = self._setup(risk="assertive", panel_cfg="all",
                                 change_after=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("FRESH", self._receipt(td))

    def test_assertive_nested_code_only_edit_blocks(self):
        # C2 GUARANTEE PROOF: with code_roots naming the nested repo, a code-only
        # edit INSIDE it after the panel stamp reads STALE and blocks the
        # assertive close. Red against pre-C2 code: the fingerprint saw only the
        # outer tree, so the nested edit was invisible → silently FRESH → close.
        d, td, env = self._setup(risk="assertive", panel_cfg="all",
                                 code_roots=["sub"], nested_change=True)
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        self.assertIn("pending", self._receipt(td))

    def test_assertive_nested_fresh_closes(self):
        # Negative control: code_roots set, but NO post-panel nested edit — the
        # stamp still matches, so the assertive close goes through FRESH. Proves
        # the block above is caused by the nested edit, not merely by code_roots.
        d, td, env = self._setup(risk="assertive", panel_cfg="all",
                                 code_roots=["sub"], change_after=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("FRESH", self._receipt(td))

    def test_assertive_stale_panel_ok_closes_with_recorded_reason(self):
        # T1: the narrow escape works for assertive too (user decision
        # 2026-08-23 — same two escapes as irreversible).
        d, td, env = self._setup(risk="assertive", panel_cfg="all")
        r = self._close(d, env, "--stale-panel-ok")
        self.assertNotIn("Task 001 done.", r.stdout)  # reason required
        r = self._close(d, env, "--stale-panel-ok", "--reason",
                        "docs-only delta, diff reviewed")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn(CLAUSE, receipt)
        self.assertIn("STALE", receipt)
        self.assertIn('accepted: "docs-only delta, diff reviewed"', receipt)

    def test_assertive_force_bypasses_and_attributes_reason_to_force(self):
        d, td, env = self._setup(risk="assertive", panel_cfg="all")
        r = self._close(d, env, "--force", "--reason", "emergency close")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn("Forced close, reason:", receipt)
        self.assertIn("emergency close", receipt)
        self.assertIn("STALE", receipt)
        self.assertNotIn('accepted: "emergency close"', receipt)

    def test_unclassified_stale_blocks_under_all(self):
        # Panel finding O1, end-to-end: under panel_required_for "all" an unset
        # `## Risk` resting on a stale carrying panel must block, or blanking
        # the field is a freshness bypass of the T1 guarantee. Red pre-O1
        # (unclassified closed advisory-only).
        d, td, env = self._setup(risk="unclassified", panel_cfg="all")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        self.assertIn("pending", self._receipt(td))

    def test_reversible_stale_still_advisory_under_all(self):
        # O1 negative control: the gate did NOT widen to reversible — a truly
        # reversible task still closes with only the advisory STALE clause.
        d, td, env = self._setup(risk="reversible", panel_cfg="all")
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))

    def test_default_list_config_asymmetry_is_pinned(self):
        # Panel finding O2: pin the SEEDED-default behavior, not just "all".
        # Under panel_required_for=["assertive","irreversible"] the freshness
        # gate fires for assertive (panel required) but is INERT for
        # unclassified (panel NOT required → resolve_panel_required False), so
        # `unclassified` does not hit the freshness BLOCK. This is the boundary
        # a future edit to resolve_panel_required or the init seed could shift
        # undetected — the documented scope limit of the O1 fix.
        DEFAULT = ["assertive", "irreversible"]
        # assertive → freshness block fires
        d, td, env = self._setup(risk="assertive", panel_cfg=DEFAULT)
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn(BLOCK_MARKER, r.stderr)
        # unclassified → gate inert (panel not required for it by default)
        d2, td2, env2 = self._setup(risk="unclassified", panel_cfg=DEFAULT)
        r2 = self._close(d2, env2)
        self.assertNotIn(BLOCK_MARKER, r2.stderr)

    def test_irreversible_stale_without_policy_is_advisory(self):
        # Judge A4: a voluntary panel in a no-policy project must not block.
        d, td, env = self._setup(risk="irreversible", panel_cfg=None)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("STALE", self._receipt(td))

    def test_no_impl_round_gets_panel_evidence_block_not_freshness(self):
        # Judge C3 / design A3: exactly one block message.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 round_head="Plan")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("panel review required by policy", r.stderr)
        self.assertNotIn(BLOCK_MARKER, r.stderr)

    def test_newest_impl_fail_round_skips_freshness_gate(self):
        # Judge C3 case 1: a stamped FAIL round cannot carry the close, so the
        # freshness gate must stay silent and the evidence block must fire.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 verdict="FAIL")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("panel review required by policy", r.stderr)
        self.assertNotIn(BLOCK_MARKER, r.stderr)

    def test_replan_over_stamped_impl_skips_freshness_gate(self):
        # Judge C3 case 2: newest round is plan → evidence does not carry.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 round_head="Plan", verdict="PASS",
                                 extra_round=None)
        # build: plan round newest (written by _setup), stamped impl BELOW it
        fp = tree_state_fingerprint(d)
        jm = td / "judge.md"
        jm.write_text(jm.read_text(encoding="utf-8")
                      + f"\n# Panel Impl Review — task 1\n\n"
                        f"**PANEL VERDICT: PASS** — 4/4, quorum 3\n"
                        f"**Tree-state:** {fp}\n\nolder impl\n", encoding="utf-8")
        r = self._close(d, env)
        self.assertNotIn("Task 001 done.", r.stdout)
        self.assertIn("panel review required by policy", r.stderr)
        self.assertNotIn(BLOCK_MARKER, r.stderr)

    def test_missing_stamp_recorded_not_silent(self):
        # Judge F4: deleting/never-having the stamp must leave a record, not
        # read as legacy silence.
        d, td, env = self._setup(risk="irreversible", panel_cfg="all",
                                 stamp=False)
        r = self._close(d, env)
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        self.assertIn("no stamp recorded", self._receipt(td))

    def test_force_bypasses_and_attributes_reason_to_force(self):
        # Judge F5 / design A8: --force keeps whole-policy semantics; the
        # shared --reason lands as the FORCED reason, and the freshness clause
        # still records STALE (Leg 1 is unconditional).
        d, td, env = self._setup(risk="irreversible", panel_cfg="all")
        r = self._close(d, env, "--force", "--reason", "emergency close")
        self.assertIn("Task 001 done.", r.stdout, r.stderr)
        receipt = self._receipt(td)
        self.assertIn("Forced close, reason:", receipt)
        self.assertIn("emergency close", receipt)
        self.assertIn("STALE", receipt)
        self.assertNotIn('accepted: "emergency close"', receipt)


if __name__ == "__main__":
    unittest.main()
