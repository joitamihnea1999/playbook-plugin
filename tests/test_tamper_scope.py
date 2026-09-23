#!/usr/bin/env python3
"""Judge tamper guard — scope and posture (task 059, PLAN.md A1.2).

The guard is the ONLY defense against a rogue or uncontained judge mutating the
repo on Windows. Task 059 makes it truthful in both directions:
  - it SEES what it could not: a project living in a SUBDIRECTORY of its repo
    (porcelain paths are toplevel-relative — review.py used to join them onto
    `project_path`, so content-only edits to already-dirty files were invisible
    and monitor churn under `<subdir>/.agent/` was a false flag), `code_roots`
    nested trees, a swapped task directory, a moved HEAD;
  - it no longer DISCARDS a paid review over a guard that merely could not run:
    named git-degradation states are CAUTIONS (printed, recorded in the round
    header, non-certifying for high-consequence closes), while proven mutations
    and an errored detector stay hard stops.

Pure stdlib unittest; every case builds a REAL git repo.
Run: python3 -m unittest tests.test_tamper_scope
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from tasks import review as R  # noqa: E402


def _git(*args, cwd, check=True, **kw):
    return subprocess.run(["git", *args], cwd=str(cwd), check=check,
                          capture_output=True, text=True, **kw)


def _repo() -> Path:
    d = Path(tempfile.mkdtemp())
    _git("init", "-q", cwd=d)
    _git("config", "user.email", "x@y.z", cwd=d)
    _git("config", "user.name", "x", cwd=d)
    _git("config", "core.quotePath", "true", cwd=d)
    return d


def _rmtree_git(case: unittest.TestCase, target: Path, *, must_vanish: bool = True):
    """Delete a tree that contains a git repo, Windows-safely.

    Git marks pack/object files READ-ONLY, so a plain `shutil.rmtree` of `.git`
    raises `WinError 5` on the windows/git-bash lane — four of these tests
    errored there. Clear the read-only bit and retry; if the OS still refuses
    (locked handles) SKIP rather than error: the logic under test is proven on
    POSIX and unit-injectable elsewhere. Same pattern as
    `test_judge_isolation.test_git_directory_deletion_is_caught`."""
    import shutil
    import stat as _stat

    def _force(func, path, _exc):
        try:
            os.chmod(path, _stat.S_IWRITE)
            func(path)
        except OSError:
            pass
    try:
        shutil.rmtree(target, onerror=_force)
    except OSError:
        case.skipTest(f"OS will not let the test delete {target.name}")
    if must_vanish and target.exists():
        case.skipTest(f"OS retained {target.name} despite rmtree (locked handles)")


def _commit_all(d: Path, msg="c"):
    _git("add", "-A", cwd=d)
    _git("commit", "-qm", msg, cwd=d)


class SubdirProject(unittest.TestCase):
    """Project = `<repo>/app`; git names paths `app/...` (toplevel-relative)."""

    def _subdir(self):
        repo = _repo()
        app = repo / "app"
        (app / ".agent" / "tasks" / "001-x").mkdir(parents=True)
        tf = app / ".agent" / "tasks" / "001-x" / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        (app / "src.py").write_text("a = 1\n", encoding="utf-8")
        _commit_all(repo)
        return repo, app, tf

    def test_content_edit_to_already_dirty_file_is_caught_project_relative(self):
        repo, app, tf = self._subdir()
        (app / "src.py").write_text("a = 2\n", encoding="utf-8")          # dirty BEFORE
        before = R._snapshot_repo_state(app, tf)
        (app / "src.py").write_text("a = 3\n", encoding="utf-8")          # judge edits content only
        changes = R._detect_tamper(app, tf, before)
        self.assertEqual(changes, ["working tree content changed: src.py"], changes)

    def test_dirty_hash_keys_are_project_relative(self):
        repo, app, tf = self._subdir()
        (app / "src.py").write_text("a = 2\n", encoding="utf-8")
        before = R._snapshot_repo_state(app, tf)
        self.assertIn("src.py", before["dirty_hashes"], before["dirty_hashes"])
        self.assertNotIn("app/src.py", before["dirty_hashes"])
        self.assertTrue(before["dirty_hashes"]["src.py"].startswith(("sha", "")))   # a real hash, not unhashed:error
        self.assertFalse(before["dirty_hashes"]["src.py"].startswith("unhashed"))

    def test_monitor_churn_under_subdir_is_not_tamper(self):
        repo, app, tf = self._subdir()
        mon = app / ".agent" / "monitor"
        mon.mkdir()
        (mon / "trace.md").write_text("t0\n", encoding="utf-8")
        before = R._snapshot_repo_state(app, tf)
        (mon / "trace.md").write_text("t0\nt1\n", encoding="utf-8")
        (mon / "session.md").write_text("s\n", encoding="utf-8")
        self.assertEqual(R._detect_tamper(app, tf, before), [])

    def test_quoted_and_renamed_paths_key_correctly(self):
        repo, app, tf = self._subdir()
        (app / "über.py").write_text("u\n", encoding="utf-8")            # git C-quotes this in readable porcelain
        (app / "old.py").write_text("o\n", encoding="utf-8")
        _commit_all(repo)
        _git("mv", "old.py", "new.py", cwd=app)                             # staged rename → R  new\0old
        (app / "über.py").write_text("u2\n", encoding="utf-8")
        before = R._snapshot_repo_state(app, tf)
        self.assertIn("über.py", before["dirty_hashes"])
        self.assertIn("new.py", before["dirty_hashes"])
        (app / "über.py").write_text("u3\n", encoding="utf-8")
        changes = R._detect_tamper(app, tf, before)
        self.assertEqual(changes, ["working tree content changed: über.py"], changes)

    def test_unresolvable_toplevel_is_a_caution_not_a_project_path_join(self):
        repo, app, tf = self._subdir()
        (app / "src.py").write_text("a = 2\n", encoding="utf-8")
        real_run = subprocess.run

        def _no_toplevel(cmd, *a, **k):
            if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(cmd, 128, b"", b"fatal")
            return real_run(cmd, *a, **k)
        with mock.patch("subprocess.run", side_effect=_no_toplevel):
            before = R._snapshot_repo_state(app, tf)
        self.assertEqual(before["dirty_hashes"], {})                          # never project_path / rel
        self.assertFalse(before.get("hash_scope_ok", True))
        full = R._detect_tamper_full(app, tf, before)
        self.assertTrue(any("hash scope" in c for c in full["cautions"]), full)
        self.assertEqual(full["mutations"], [])


class CodeRoots(unittest.TestCase):
    def _with_root(self):
        proj = _repo()
        (proj / ".agent" / "tasks" / "001-x").mkdir(parents=True)
        tf = proj / ".agent" / "tasks" / "001-x" / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        (proj / ".gitignore").write_text("nested/\n", encoding="utf-8")
        (proj / ".agent" / "config.json").write_text('{"code_roots": ["nested"]}', encoding="utf-8")
        nested = proj / "nested"
        nested.mkdir()
        _git("init", "-q", cwd=nested)
        _git("config", "user.email", "x@y.z", cwd=nested)
        _git("config", "user.name", "x", cwd=nested)
        (nested / "x.py").write_text("x = 1\n", encoding="utf-8")
        _commit_all(nested)
        _commit_all(proj)
        return proj, nested, tf

    def test_edit_inside_nested_root_is_caught(self):
        proj, nested, tf = self._with_root()
        before = R._snapshot_repo_state(proj, tf)
        self.assertIn("nested", before["roots"])
        (nested / "x.py").write_text("x = 2\n", encoding="utf-8")
        full = R._detect_tamper_full(proj, tf, before)
        self.assertTrue(any("code root nested" in m and "x.py" in m for m in full["mutations"]), full)

    def test_root_git_deleted_mid_review_is_a_mutation(self):
        proj, nested, tf = self._with_root()
        before = R._snapshot_repo_state(proj, tf)
        _rmtree_git(self, nested / ".git")
        full = R._detect_tamper_full(proj, tf, before)
        self.assertTrue(any("nested" in m and "unreadable" in m for m in full["mutations"]), full)

    def test_root_symlink_retargeted_to_same_head_clone_is_a_mutation(self):
        proj, nested, tf = self._with_root()
        if not hasattr(os, "symlink"):
            self.skipTest("no symlink")
        # Make `nested` a symlink to clone A; retarget to clone B at the same HEAD.
        base = Path(tempfile.mkdtemp())
        a, b = base / "a", base / "b"
        _git("clone", "-q", str(nested), str(a), cwd=base)
        _git("clone", "-q", str(nested), str(b), cwd=base)
        _rmtree_git(self, nested)
        try:
            os.symlink(a, nested, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted")
        before = R._snapshot_repo_state(proj, tf)
        os.unlink(nested)
        os.symlink(b, nested, target_is_directory=True)
        full = R._detect_tamper_full(proj, tf, before)
        self.assertTrue(any("nested" in m and "identity" in m for m in full["mutations"]), full)

    def test_unchanged_root_is_not_flagged(self):
        # Negative control: a nested root nobody touched produces nothing.
        proj, nested, tf = self._with_root()
        before = R._snapshot_repo_state(proj, tf)
        full = R._detect_tamper_full(proj, tf, before)
        self.assertEqual(full["mutations"], [], full)

    def test_head_moved_is_a_caution_not_a_mutation(self):
        proj, nested, tf = self._with_root()
        before = R._snapshot_repo_state(proj, tf)
        self.assertEqual(before["head"]["state"], "ok")
        (proj / "note.md").write_text("n\n", encoding="utf-8")
        _commit_all(proj, "concurrent")
        with mock.patch("provider.sandbox.containment_available", return_value=True):
            full = R._detect_tamper_full(proj, tf, before)
        self.assertTrue(any("head moved" in c.lower() for c in full["cautions"]), full)
        # the new commit also cleaned the tree of an untracked file → porcelain differs; that part IS a mutation-class line
        self.assertFalse(any("head" in m.lower() for m in full["mutations"]), full)

    def test_head_read_error_is_a_caution(self):
        proj, nested, tf = self._with_root()
        real_run = subprocess.run

        def _no_head(cmd, *a, **k):
            if cmd[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(cmd, 128, b"", b"fatal")
            return real_run(cmd, *a, **k)
        with mock.patch("subprocess.run", side_effect=_no_head):
            before = R._snapshot_repo_state(proj, tf)
        self.assertEqual(before["head"]["state"], "error")
        full = R._detect_tamper_full(proj, tf, before)
        self.assertTrue(any("HEAD" in c for c in full["cautions"]), full)


class TaskDirIdentity(unittest.TestCase):
    def test_task_dir_swapped_for_symlink_is_a_mutation(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlink")
        d = _repo()
        td = d / ".agent" / "tasks" / "001-x"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        _commit_all(d)
        before = R._snapshot_repo_state(d, tf)
        self.assertIn("taskdir_identity", before)
        elsewhere = Path(tempfile.mkdtemp()) / "001-x"
        elsewhere.mkdir()
        (elsewhere / "task.md").write_text("gate\n", encoding="utf-8")       # identical content
        _rmtree_git(self, td)
        try:
            os.symlink(elsewhere, td, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted")
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("task directory identity" in m for m in full["mutations"]), full)


class TaskDirIdentityNegative(unittest.TestCase):
    def test_unchanged_task_dir_is_not_flagged(self):
        # Negative control for the identity signal: an untouched task dir.
        d = _repo()
        td = d / ".agent" / "tasks" / "001-x"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        _commit_all(d)
        before = R._snapshot_repo_state(d, tf)
        self.assertEqual(R._detect_tamper_full(d, tf, before)["mutations"], [])


class DetectorSplit(unittest.TestCase):
    def _clean(self):
        d = _repo()
        tf = d / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        _commit_all(d)
        return d, tf

    def test_z_failure_is_a_caution_and_kept_in_legacy_list(self):
        d, tf = self._clean()
        before = {"porcelain": "", "task_hash": None, "dirty_hashes": {}, "z_read_ok": False, "is_git": True}
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("degraded" in c for c in full["cautions"]), full)
        self.assertEqual(full["mutations"], [])
        self.assertTrue(any("degraded" in c for c in R._detect_tamper(d, tf, before)))   # legacy union unchanged

    def test_git_dir_deletion_is_a_mutation(self):
        d, tf = self._clean()
        before = R._snapshot_repo_state(d, tf)
        _rmtree_git(self, d / ".git")
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("unreadable" in m for m in full["mutations"]), full)

    def test_detector_exception_is_a_mutation_class_hard_stop(self):
        d, tf = self._clean()
        before = R._snapshot_repo_state(d, tf)
        with mock.patch.object(R, "_snapshot_repo_state", side_effect=RuntimeError("boom")):
            full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("UNVERIFIED" in m for m in full["mutations"]), full)
        self.assertEqual(full["cautions"], [])

    def test_degraded_notice_text(self):
        txt = R._degraded_notice(["content-hash guard degraded: x"])
        self.assertIn("tamper guard degraded", txt)
        self.assertIn("KEPT", txt)
        self.assertIn("UNVERIFIED", txt)
        self.assertNotIn("TAMPER DETECTED", txt)


class _Project(unittest.TestCase):
    """A git-initialised playbook project with one task, a models.json panel of
    two claude seats, and a MIND_MAP — enough to drive the real panel and
    single-judge entry points with faked judges."""

    def setUp(self):
        import json, shutil
        self.d = _repo()
        self.addCleanup(lambda: shutil.rmtree(self.d, ignore_errors=True))
        td = self.d / ".agent" / "tasks" / "001-demo"
        td.mkdir(parents=True)
        self.tf = td / "task.md"
        self.tf.write_text(
            "# 001 - demo\n\n## Status\npending\n\n## Intent\nx\n\n"
            "## Plan Review\n(plan review triage appears here)\n\n"
            "## Design Phase\n- [ ] a gate\n", encoding="utf-8")
        (self.d / ".agent" / "models.json").write_text(json.dumps(
            {"panel": ["claude:opus", "claude:sonnet"], "default_judge": "claude:opus"}), encoding="utf-8")
        (self.d / "MIND_MAP.md").write_text("# Mind Map\n[1] node\n", encoding="utf-8")
        _commit_all(self.d)
        R._PB_JOURNAL_MOD = None
        R._PB_JOURNAL_LOADED = False

    def _degraded(self, *a, **k):
        return {"mutations": [],
                "cautions": ["content-hash guard degraded: could not enumerate dirty files at close (`git status -z` failed)"],
                "degraded": True}

    def _mutated(self, *a, **k):
        return {"mutations": ["working tree: ?? rogue.md"], "cautions": [], "degraded": False}


class PanelDegradedKeepsVerdict(_Project):
    def _run_panel(self):
        import contextlib, io
        from provider.adapters.claude import ClaudeAdapter
        err, out = io.StringIO(), io.StringIO()
        code = None
        cwd = os.getcwd()
        with mock.patch.object(ClaudeAdapter, "is_available", classmethod(lambda cls: True)), \
             mock.patch.object(ClaudeAdapter, "run_headless_judge", lambda self, **kw: "1. **Note** — fine.\n"), \
             mock.patch.object(R, "_detect_tamper_full", side_effect=self._degraded):
            os.chdir(self.d)
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                    R.cmd_panel_review(["001", "--models", "claude:opus,claude:sonnet"])
            except SystemExit as e:
                code = e.code
            finally:
                os.chdir(cwd)
        return code, err.getvalue(), out.getvalue()

    def test_degraded_panel_is_kept_and_recorded(self):
        code, err, out = self._run_panel()
        self.assertIn(code, (None, 0), (code, err))
        self.assertNotIn("TAMPER DETECTED", err)
        self.assertIn("tamper guard degraded", err)
        jm = (self.tf.parent / "judge.md").read_text(encoding="utf-8")
        self.assertIn("PANEL VERDICT", jm)
        self.assertRegex(jm, r"\*\*Tamper guard:\*\* degraded — .*git status -z")

    def test_clean_panel_records_clean(self):
        import contextlib, io
        from provider.adapters.claude import ClaudeAdapter
        cwd = os.getcwd()
        with mock.patch.object(ClaudeAdapter, "is_available", classmethod(lambda cls: True)), \
             mock.patch.object(ClaudeAdapter, "run_headless_judge", lambda self, **kw: "1. **Note** — fine.\n"):
            os.chdir(self.d)
            try:
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.suppress(SystemExit):
                        R.cmd_panel_review(["001", "--models", "claude:opus,claude:sonnet"])
            finally:
                os.chdir(cwd)
        jm = (self.tf.parent / "judge.md").read_text(encoding="utf-8")
        self.assertIn("**Tamper guard:** clean", jm)


class SingleDegradedKeepsFindings(_Project):
    def _drive(self, detect):
        import contextlib, io
        from tasks import cli as tcli
        argv = ["tasks", "plan-review", "001", "--backend", "claude"]
        err, out = io.StringIO(), io.StringIO()
        code = None
        cwd = os.getcwd()

        def fake_run(agent, agent_args, **kw):
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="1. **Note** — fine.\n", stderr="")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch("shutil.which", return_value="/usr/bin/claude"))
            stack.enter_context(mock.patch("provider.sandbox.run", side_effect=fake_run))
            stack.enter_context(mock.patch("provider.sandbox.format_judge_output", side_effect=lambda r: (r.stdout or "")))
            stack.enter_context(mock.patch.object(R, "_detect_tamper_full", side_effect=detect))
            os.chdir(self.d)
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                    tcli.main()
            except SystemExit as e:
                code = e.code
            finally:
                os.chdir(cwd)
        return code, err.getvalue(), out.getvalue()

    def test_degraded_single_review_is_kept_and_marked(self):
        code, err, out = self._drive(self._degraded)
        self.assertIn(code, (None, 0), (code, err))
        self.assertNotIn("TAMPER DETECTED", err)
        self.assertIn("tamper guard degraded", err)
        log = (self.tf.parent / R._judge_log_name("claude")).read_text(encoding="utf-8")
        self.assertRegex(log, r"\[tamper guard\] degraded — .*git status -z")
        task = self.tf.read_text(encoding="utf-8")
        self.assertIn("Tamper guard: degraded", task)             # the findings block carries the mark too

    def test_mutated_single_review_still_hard_stops(self):
        code, err, out = self._drive(self._mutated)
        self.assertEqual(code, 1)
        self.assertIn("TAMPER DETECTED", err)
        self.assertFalse((self.tf.parent / R._judge_log_name("claude")).exists())


class TailCertFailsClosed(unittest.TestCase):
    def _repo_with_snapshot(self):
        from tasks.core import build_panel_snapshot, tree_state_fingerprint
        d = _repo()
        (d / "code.py").write_text("x = 1\n", encoding="utf-8")
        (d / "docs").mkdir()
        _commit_all(d)
        snap = build_panel_snapshot(d, tree_state_fingerprint(d))
        (d / "docs" / "g.md").write_text("# d\n", encoding="utf-8")
        return d, snap

    def _raw_pass(self, project_path, prompt, timeout_secs):
        import re as _re
        n = _re.search(r"TAIL-CERT ([0-9a-f]+):", prompt).group(1)
        return f"TAIL-CERT {n}: PASS\n"

    def test_raising_snapshot_returns_none(self):
        d, snap = self._repo_with_snapshot()
        with mock.patch.object(R, "_run_tail_cert_judge_raw", side_effect=self._raw_pass), \
             mock.patch.object(R, "_snapshot_repo_state", side_effect=RuntimeError("boom")):
            v = R.run_tail_cert_judge(d, snap, ["docs/g.md"], "PANEL PASS")
        self.assertIsNone(v)

    def test_degraded_guard_returns_none(self):
        d, snap = self._repo_with_snapshot()
        degraded = {"mutations": [], "cautions": ["content-hash guard degraded: x"]}
        with mock.patch.object(R, "_run_tail_cert_judge_raw", side_effect=self._raw_pass), \
             mock.patch.object(R, "_detect_tamper_full", return_value=degraded):
            v = R.run_tail_cert_judge(d, snap, ["docs/g.md"], "PANEL PASS")
        self.assertIsNone(v)

    def test_clean_guard_still_certifies(self):
        d, snap = self._repo_with_snapshot()
        with mock.patch.object(R, "_run_tail_cert_judge_raw", side_effect=self._raw_pass):
            v = R.run_tail_cert_judge(d, snap, ["docs/g.md"], "PANEL PASS")
        self.assertIsNotNone(v)


class TaskDirExemption(unittest.TestCase):
    """W4 (T045 + plan panel P5/codex-high #3): the untracked-task-dir exemption
    fires only when task.md ITSELF is untracked; when task.md is tracked, only
    the panel's own record files (by name) are exempt, and a rogue sibling
    flags. A failed trackedness probe is a caution and narrows the exemption."""

    def _proj(self, *, commit_task: bool):
        d = _repo()
        td = d / ".agent" / "tasks" / "001-x"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        if commit_task:
            _commit_all(d)
        return d, td, tf

    def test_snapshot_records_trackedness(self):
        d, td, tf = self._proj(commit_task=True)
        self.assertEqual(R._snapshot_repo_state(d, tf)["taskmd_tracked"], "tracked")
        d2, td2, tf2 = self._proj(commit_task=False)
        self.assertEqual(R._snapshot_repo_state(d2, tf2)["taskmd_tracked"], "untracked")

    def test_tracked_taskmd_rogue_sibling_flags(self):
        d, td, tf = self._proj(commit_task=True)
        (td / "judge.md").write_text("old\n", encoding="utf-8")          # an untracked record child exists
        before = R._snapshot_repo_state(d, tf)
        (td / "evil.py").write_text("x\n", encoding="utf-8")
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("evil.py" in m for m in full["mutations"]), full)

    def test_tracked_taskmd_record_files_are_exempt(self):
        d, td, tf = self._proj(commit_task=True)
        before = R._snapshot_repo_state(d, tf)
        (td / "judge.md").write_text("round\n", encoding="utf-8")
        (td / "judge-codex.log").write_text("log\n", encoding="utf-8")      # REAL names only
        (td / "judge.partial.log").write_text("p\n", encoding="utf-8")
        (td / "judge-archive.md").write_text("a\n", encoding="utf-8")
        full = R._detect_tamper_full(d, tf, before)
        self.assertEqual(full["mutations"], [], full)

    def test_tracked_record_file_MODIFIED_still_flags(self):
        # The pairing that keeps the exemption honest: a NEWLY CREATED record
        # file is the panel's own churn (exempt), but a COMMITTED judge.md that
        # a judge rewrites during the window is a rogue editing evidence.
        d, td, tf = self._proj(commit_task=False)
        (td / "judge.md").write_text("v1\n", encoding="utf-8")
        _commit_all(d)                                   # task.md AND judge.md tracked
        before = R._snapshot_repo_state(d, tf)
        self.assertEqual(before["taskmd_tracked"], "tracked")
        (td / "judge.md").write_text("v1\nrogue\n", encoding="utf-8")
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("judge.md" in m for m in full["mutations"]), full)

    def test_untracked_taskmd_keeps_the_broad_exemption(self):
        d, td, tf = self._proj(commit_task=False)
        before = R._snapshot_repo_state(d, tf)
        (td / "anything.txt").write_text("record churn\n", encoding="utf-8")
        full = R._detect_tamper_full(d, tf, before)
        self.assertEqual(full["mutations"], [], full)

    def test_probe_failure_is_a_caution_and_narrows(self):
        d, td, tf = self._proj(commit_task=True)
        real_run = subprocess.run

        def _no_lsfiles(cmd, *a, **k):
            if cmd[:2] == ["git", "ls-files"] or (len(cmd) > 3 and cmd[3] == "ls-files"):
                raise OSError("git ls-files unavailable")
            return real_run(cmd, *a, **k)
        with mock.patch("subprocess.run", side_effect=_no_lsfiles):
            before = R._snapshot_repo_state(d, tf)
        self.assertEqual(before["taskmd_tracked"], "unknown")
        (td / "evil.py").write_text("x\n", encoding="utf-8")
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("evil.py" in m for m in full["mutations"]), full)
        self.assertTrue(any("tracked" in c for c in full["cautions"]), full)


class GitDiscoveryBoundaries(unittest.TestCase):
    """W5 (T009 #2): `_in_git_repo` honours GIT_CEILING_DIRECTORIES, GIT_DIR and
    filesystem boundaries the way git's own discovery does."""

    def test_ceiling_stops_the_walk(self):
        # git's OWN semantics, measured on git 2.54.0 (059 impl-panel r1, opus F2
        # suggested the ceiling dir should still be probed): with the ceiling set
        # to the directory that HOLDS .git, `git rev-parse --is-inside-work-tree`
        # run from a child says "not a git repository" — git does NOT examine the
        # ceiling's own .git while walking up. Run from the ceiling itself it
        # succeeds, because the start directory is always examined. This test
        # pins that behaviour, not our implementation's convenience.
        repo = _repo()
        app = repo / "app"
        app.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(repo)}):
            self.assertFalse(R._in_git_repo(app))          # `.git` is AT the ceiling → not entered
            self.assertTrue(R._in_git_repo(repo))           # the start dir itself is always probed
        self.assertTrue(R._in_git_repo(app))                # no ceiling → found

    def test_git_dir_env_means_repo(self):
        d = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"GIT_DIR": "/somewhere/.git"}):
            self.assertTrue(R._in_git_repo(d))

    def test_empty_ceiling_entries_are_ignored(self):
        repo = _repo()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": os.pathsep}):
            self.assertTrue(R._in_git_repo(repo / "app"))  # a bogus/empty list must not disable discovery


class CodexTempLog(unittest.TestCase):
    """W6 (T008 #5): the codex `-o` transcript is owned from allocation to every
    exit — normal, exception and SystemExit — so no `*-judge-codex.log` is left
    in the system temp dir by a timeout/budget/dead-pin/tamper exit."""

    def test_manager_unlinks_on_every_exit(self):
        for exc in (None, RuntimeError("boom"), SystemExit(1)):
            with self.subTest(exc=type(exc).__name__ if exc else "normal"):
                try:
                    with R._TempPath(suffix="-judge-codex.log") as p:
                        self.assertTrue(p.exists())
                        p.write_text("transcript", encoding="utf-8")
                        if exc is not None:
                            raise exc
                except (RuntimeError, SystemExit):
                    pass
                self.assertFalse(p.exists(), p)

    def test_manager_tolerates_an_already_removed_file(self):
        with R._TempPath(suffix="-judge-codex.log") as p:
            p.unlink()
        self.assertFalse(p.exists())

    def test_registry_is_drained_on_a_sys_exit_through_the_command(self):
        # The real shape: the codex branch allocates, then the body exits via
        # sys.exit (budget / timeout / dead-pin / tamper). `atexit` would not
        # fire in-process, so the registry drain in cmd_single_review's finally
        # is what unlinks it.
        held = R._TempPath(suffix="-judge-codex.log")
        p = held.keep_until_exit()
        self.assertTrue(p.exists())
        with mock.patch.object(R, "_cmd_single_review", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                R.cmd_single_review("plan-review", ["001"])
        self.assertFalse(p.exists(), "the codex -o transcript leaked past a sys.exit")

    def test_no_codex_transcript_survives_a_budget_exit(self):
        # End to end through the real dispatch: a fake codex whose output is the
        # budget-exhausted message drives cmd_single_review to its nonzero exit.
        import glob, tempfile as _tf
        d = _repo()
        td = d / ".agent" / "tasks" / "001-x"
        td.mkdir(parents=True)
        (td / "task.md").write_text(
            "# 001 - x\n\n## Status\npending\n\n## Intent\nx\n\n"
            "## Plan Review\n(plan review triage appears here)\n\n"
            "## Design Phase\n- [ ] a gate\n", encoding="utf-8")
        (d / "MIND_MAP.md").write_text("# Mind Map\n[1] node\n", encoding="utf-8")
        _commit_all(d)
        pattern = os.path.join(_tf.gettempdir(), "*-judge-codex.log")
        before_files = set(glob.glob(pattern))

        def fake_run(agent, args, **kw):
            return subprocess.CompletedProcess(
                args=[], returncode=1,
                stdout="Credit balance is too low to run this request.", stderr="")
        cwd = os.getcwd()
        import contextlib, io
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("shutil.which", return_value="/usr/bin/codex"))
            stack.enter_context(mock.patch("provider.sandbox.run", side_effect=fake_run))
            stack.enter_context(mock.patch("provider.sandbox.format_judge_output",
                                           side_effect=lambda r: (r.stdout or "")))
            os.chdir(d)
            try:
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.suppress(SystemExit):
                        R.cmd_single_review("plan-review", ["001", "--backend", "codex"])
            finally:
                os.chdir(cwd)
        self.assertEqual(set(glob.glob(pattern)) - before_files, set(),
                         "a codex -o transcript was left in the system temp dir")


class ConcurrentCommitDuringReview(unittest.TestCase):
    """059 impl-panel r1 (opus F1): committing already-dirty files during a
    (background) panel REMOVES their porcelain lines. A read-only judge cannot
    commit, so those removals are the concurrent actor's, not tamper — they must
    not void the paid panel. New lines and content changes still flag."""

    def _proj(self):
        d = _repo()
        (d / "a.py").write_text("a = 1\n", encoding="utf-8")
        (d / "b.py").write_text("b = 1\n", encoding="utf-8")
        _commit_all(d)
        tf = d / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        (d / ".gitignore").write_text("task.md\n", encoding="utf-8")
        _commit_all(d)
        return d, tf

    def test_commit_of_dirty_files_is_a_caution_not_a_mutation(self):
        d, tf = self._proj()
        (d / "a.py").write_text("a = 2\n", encoding="utf-8")       # dirty BEFORE the panel
        before = R._snapshot_repo_state(d, tf)
        _commit_all(d, "the agent commits its own work mid-panel")
        # Containment pinned (059 impl-panel r3, grok #2): the demotion applies
        # only where an OS sandbox denies judge writes, so without this mock the
        # case inverts on the Windows lane — which HAS no containment.
        with mock.patch("provider.sandbox.containment_available", return_value=True):
            full = R._detect_tamper_full(d, tf, before)
        self.assertEqual(full["mutations"], [], full)
        self.assertTrue(any("head moved" in c.lower() for c in full["cautions"]), full)
        self.assertTrue(any("no longer listed" in c.lower() or "removed" in c.lower()
                            for c in full["cautions"]), full)

    def test_a_new_file_during_that_commit_window_still_flags(self):
        d, tf = self._proj()
        (d / "a.py").write_text("a = 2\n", encoding="utf-8")
        before = R._snapshot_repo_state(d, tf)
        _commit_all(d, "concurrent commit")
        (d / "rogue.md").write_text("fabricated\n", encoding="utf-8")   # the judge's own write
        with mock.patch("provider.sandbox.containment_available", return_value=True):
            full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("rogue.md" in m for m in full["mutations"]), full)

    def test_content_change_during_that_window_still_flags(self):
        d, tf = self._proj()
        (d / "a.py").write_text("a = 2\n", encoding="utf-8")
        (d / "b.py").write_text("b = 2\n", encoding="utf-8")
        before = R._snapshot_repo_state(d, tf)
        subprocess.run(["git", "add", "a.py"], cwd=str(d), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "commit only a.py"], cwd=str(d), check=True, capture_output=True)
        (d / "b.py").write_text("b = 3\n", encoding="utf-8")            # judge edits the still-dirty file
        with mock.patch("provider.sandbox.containment_available", return_value=True):
            full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("b.py" in m for m in full["mutations"]), full)

    def test_removal_without_a_commit_is_still_a_mutation(self):
        # Negative control for the demotion: HEAD did NOT move, so a vanished
        # untracked file is a judge deleting it.
        d, tf = self._proj()
        (d / "scratch.txt").write_text("x\n", encoding="utf-8")
        before = R._snapshot_repo_state(d, tf)
        (d / "scratch.txt").unlink()
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("scratch.txt" in m for m in full["mutations"]), full)


class OneSidedStatusFailure(unittest.TestCase):
    """059 impl-panel r1 (grok #2): a one-sided readable-`git status` failure
    was BOTH the named caution and the `.git removed` MUTATION, so a degraded
    guard still aborted — the T009 harm this task exists to remove. The
    mutation now needs a real `.git` disappearance."""

    def _proj(self):
        d = _repo()
        tf = d / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        _commit_all(d)
        return d, tf

    def test_status_failure_with_git_intact_is_caution_only(self):
        d, tf = self._proj()
        before = R._snapshot_repo_state(d, tf)
        real_run = subprocess.run

        def _fail_status(cmd, *a, **k):
            if "status" in cmd:
                return subprocess.CompletedProcess(cmd, 1, b"" if "-z" in cmd else "", b"fatal" if "-z" in cmd else "fatal")
            return real_run(cmd, *a, **k)
        with mock.patch("subprocess.run", side_effect=_fail_status):
            full = R._detect_tamper_full(d, tf, before)
        self.assertEqual(full["mutations"], [], full)
        self.assertTrue(any("did not run" in c for c in full["cautions"]), full)

    def test_real_git_deletion_is_still_a_mutation(self):
        d, tf = self._proj()
        before = R._snapshot_repo_state(d, tf)
        _rmtree_git(self, d / ".git")
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("unreadable" in m for m in full["mutations"]), full)


class RecordFileContentChurn(unittest.TestCase):
    """059 impl-panel r1 (grok #1): with task.md TRACKED, an already-untracked
    `judge.md` that a SECOND panel round appends to changed only its content
    hash — exempt from the line diff but not from the content compare — so
    round 2 of every such task would have discarded its own paid verdict."""

    def _proj(self):
        d = _repo()
        td = d / ".agent" / "tasks" / "001-x"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        (d / ".gitignore").write_text("", encoding="utf-8")
        _commit_all(d)                                   # task.md tracked
        return d, td, tf

    def test_second_round_appending_to_an_untracked_judge_md_is_not_tamper(self):
        d, td, tf = self._proj()
        (td / "judge.md").write_text("round 1\n", encoding="utf-8")     # untracked record from round 1
        before = R._snapshot_repo_state(d, tf)
        (td / "judge.md").write_text("round 2\nround 1\n", encoding="utf-8")
        full = R._detect_tamper_full(d, tf, before)
        self.assertEqual(full["mutations"], [], full)

    def test_committed_judge_md_edited_is_still_a_mutation(self):
        d, td, tf = self._proj()
        (td / "judge.md").write_text("round 1\n", encoding="utf-8")
        _commit_all(d)                                   # judge.md now TRACKED
        before = R._snapshot_repo_state(d, tf)
        (td / "judge.md").write_text("rogue\n", encoding="utf-8")
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("judge.md" in m for m in full["mutations"]), full)

    def test_a_non_record_untracked_file_content_change_still_flags(self):
        d, td, tf = self._proj()
        (td / "notes.py").write_text("v1\n", encoding="utf-8")
        before = R._snapshot_repo_state(d, tf)
        (td / "notes.py").write_text("v2\n", encoding="utf-8")
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("notes.py" in m for m in full["mutations"]), full)


class NestedRootExemptions(unittest.TestCase):
    """059 impl-panel r1 (sonnet #2): the monitor / model-catalog exemptions were
    built for the outer scope only, so sanctioned concurrent churn INSIDE a
    `code_roots` root read as a MUTATION and would discard a paid panel."""

    def _with_root(self):
        proj = _repo()
        (proj / ".agent" / "tasks" / "001-x").mkdir(parents=True)
        tf = proj / ".agent" / "tasks" / "001-x" / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        (proj / ".gitignore").write_text("nested/\n", encoding="utf-8")
        (proj / ".agent" / "config.json").write_text('{"code_roots": ["nested"]}', encoding="utf-8")
        nested = proj / "nested"
        nested.mkdir()
        _git("init", "-q", cwd=nested)
        _git("config", "user.email", "x@y.z", cwd=nested)
        _git("config", "user.name", "x", cwd=nested)
        (nested / "x.py").write_text("x = 1\n", encoding="utf-8")
        _commit_all(nested)
        _commit_all(proj)
        return proj, nested, tf

    def test_monitor_churn_inside_a_root_is_not_tamper(self):
        proj, nested, tf = self._with_root()
        mon = nested / ".agent" / "monitor"
        mon.mkdir(parents=True)
        (mon / "trace.md").write_text("t0\n", encoding="utf-8")
        before = R._snapshot_repo_state(proj, tf)
        (mon / "trace.md").write_text("t0\nt1\n", encoding="utf-8")
        (mon / "session.md").write_text("s\n", encoding="utf-8")
        full = R._detect_tamper_full(proj, tf, before)
        self.assertEqual(full["mutations"], [], full)

    def test_catalog_write_inside_a_root_is_not_tamper(self):
        proj, nested, tf = self._with_root()
        (nested / ".agent").mkdir(parents=True, exist_ok=True)
        before = R._snapshot_repo_state(proj, tf)
        (nested / ".agent" / "model-catalog.json").write_text("{}\n", encoding="utf-8")
        full = R._detect_tamper_full(proj, tf, before)
        self.assertEqual(full["mutations"], [], full)

    def test_a_real_edit_inside_a_root_still_flags(self):
        proj, nested, tf = self._with_root()
        before = R._snapshot_repo_state(proj, tf)
        (nested / "x.py").write_text("x = 99\n", encoding="utf-8")
        full = R._detect_tamper_full(proj, tf, before)
        self.assertTrue(any("x.py" in m for m in full["mutations"]), full)


class Round3Fixes(unittest.TestCase):
    """059 impl-panel round 3 — opus and sonnet independently found the same
    Critical: the review-START `git status` failure branch omitted
    `degraded = True` (its close-side twin has it), so a round whose BEFORE
    snapshot could not read the tree recorded `**Tamper guard:** clean` and the
    whole close-gate protection never fired."""

    def _repo_with_task(self):
        d = _repo()
        tf = d / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        _commit_all(d)
        return d, tf

    def test_status_failure_at_review_START_marks_the_round_degraded(self):
        # ISOLATED on purpose. Both judges called this Critical by static
        # reading; measured, the branch is masked today — a one-sided porcelain
        # None also trips the transition caution, which does set the flag. The
        # only input that reaches the start branch ALONE is: both porcelain
        # None, `is_git` true at start and false at close. So the defect is a
        # latent asymmetry, not a live bypass — and it is fixed anyway, because
        # the pair must not drift.
        before = {"porcelain": None, "task_hash": None, "dirty_hashes": {},
                  "z_read_ok": True, "is_git": True, "hash_scope_ok": True}
        after = {"porcelain": None, "task_hash": None, "dirty_hashes": {},
                 "z_read_ok": True, "is_git": False, "hash_scope_ok": True}
        muts, cautions, degraded = R._scope_changes(before, after)
        self.assertEqual(muts, [])
        self.assertTrue(any("review start" in c for c in cautions), cautions)
        self.assertTrue(degraded,
                        "a review-start git-status failure must mark the GUARD degraded")

    def test_one_sided_status_failure_is_degraded_via_the_transition_caution(self):
        # The masking path, pinned so the isolation above stays meaningful.
        d, tf = self._repo_with_task()
        before = {"porcelain": None, "task_hash": None, "dirty_hashes": {},
                  "z_read_ok": True, "is_git": True, "hash_scope_ok": True}
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(full["degraded"], full)
        self.assertEqual(full["mutations"], [], full)

    def test_status_failure_at_CLOSE_marks_the_round_degraded(self):
        # The symmetric twin, so the pair can never drift apart again.
        d, tf = self._repo_with_task()
        before = R._snapshot_repo_state(d, tf)
        real_run = subprocess.run

        def _close_status_fails(cmd, *a, **k):
            if "status" in cmd:
                return subprocess.CompletedProcess(cmd, 1, b"" if "-z" in cmd else "", b"fatal")
            return real_run(cmd, *a, **k)
        with mock.patch("subprocess.run", side_effect=_close_status_fails):
            full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(full["degraded"], full)

    def test_every_degradation_caution_sets_the_flag(self):
        # Structural: no caution whose text says the guard was degraded may leave
        # `degraded` False (the drift this round caught).
        import inspect
        src = inspect.getsource(R._scope_changes)
        blocks = [b for b in src.split("cautions.append(") if "guard degraded" in b[:200]]
        self.assertTrue(blocks)
        for b in blocks:
            self.assertIn("degraded = True", b[:900], b[:300])

    def test_single_review_mark_never_claims_clean_while_cautions_exist(self):
        # opus #2: the findings/log stamp is emitted BECAUSE cautions exist, so it
        # must never read "clean" — the two durable records would contradict.
        from tasks.review import _tamper_mark_text
        self.assertEqual(_tamper_mark_text({"cautions": [], "degraded": False}), "clean")
        txt = _tamper_mark_text({"cautions": ["head moved during review (a→b)"], "degraded": False})
        self.assertNotEqual(txt, "clean")
        self.assertIn("head moved", txt)
        self.assertIn("degraded", _tamper_mark_text(
            {"cautions": ["content-hash guard degraded: x"], "degraded": True}))


class Round2Fixes(unittest.TestCase):
    """059 impl-panel round 2."""

    def _proj(self):
        d = _repo()
        (d / "a.py").write_text("a = 1\n", encoding="utf-8")
        tf = d / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        (d / ".gitignore").write_text("task.md\n", encoding="utf-8")
        _commit_all(d)
        return d, tf

    def test_z_succeeding_while_readable_status_fails_is_a_caution_not_a_crash(self):
        # grok #3: `hash_scope_ok` stayed True with `toplevel` None, so
        # `Path(None) / rel` raised TypeError → the detector turned a degraded
        # guard into a MUTATION ("review UNVERIFIED") and discarded a paid panel.
        d, tf = self._proj()
        (d / "a.py").write_text("a = 2\n", encoding="utf-8")
        real_run = subprocess.run

        def _readable_fails(cmd, *a, **k):
            if "status" in cmd and "-z" not in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "fatal: timeout")
            return real_run(cmd, *a, **k)
        with mock.patch("subprocess.run", side_effect=_readable_fails):
            snap = R._snapshot_repo_state(d, tf)          # must not raise (it did: Path(None) / rel)
        # The toplevel is now resolved whenever hashing could run, so the `-z`
        # side still produces real hashes instead of being skipped; the
        # invariant that matters is that `hash_scope_ok` and `toplevel` agree.
        self.assertEqual(snap["hash_scope_ok"], snap["prefix"] is not None)
        if snap["hash_scope_ok"]:
            self.assertIn("a.py", snap["dirty_hashes"])
        else:
            self.assertEqual(snap["dirty_hashes"], {})

    def test_uncontained_host_treats_a_moved_head_as_a_mutation(self):
        # grok #1: where the OS sandbox cannot deny writes, a judge can commit
        # its own changes — the demotion would launder them.
        d, tf = self._proj()
        before = R._snapshot_repo_state(d, tf)
        (d / "rogue.py").write_text("x\n", encoding="utf-8")
        _commit_all(d, "a judge commits its own write")
        with mock.patch("provider.sandbox.containment_available", return_value=False):
            full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("HEAD" in m or "head" in m for m in full["mutations"]), full)

    def test_contained_host_keeps_the_commit_caution(self):
        d, tf = self._proj()
        before = R._snapshot_repo_state(d, tf)
        (d / "note.md").write_text("x\n", encoding="utf-8")
        _commit_all(d, "concurrent commit")
        with mock.patch("provider.sandbox.containment_available", return_value=True):
            full = R._detect_tamper_full(d, tf, before)
        self.assertEqual(full["mutations"], [], full)
        self.assertTrue(any("head moved" in c.lower() for c in full["cautions"]), full)

    def test_a_removal_the_commit_does_not_explain_stays_a_mutation(self):
        # opus F1: the demotion was unconditional — a rogue could move HEAD and
        # launder unrelated deletions. Only paths the commit actually touched
        # are demoted.
        d, tf = self._proj()
        (d / "a.py").write_text("a = 2\n", encoding="utf-8")          # dirty, will be committed
        (d / "scratch.txt").write_text("s\n", encoding="utf-8")        # untracked, will be DELETED
        before = R._snapshot_repo_state(d, tf)
        subprocess.run(["git", "add", "a.py"], cwd=str(d), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "commit a.py only"], cwd=str(d), check=True, capture_output=True)
        (d / "scratch.txt").unlink()                                   # not part of the commit
        with mock.patch("provider.sandbox.containment_available", return_value=True):
            full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("scratch.txt" in m for m in full["mutations"]), full)
        self.assertFalse(any("scratch.txt" in c for c in full["cautions"]), full)

    def test_head_movement_alone_does_not_mark_the_round_degraded(self):
        # opus F2: a concurrent commit marked the round `degraded`, which at close
        # replaced FRESH/STALE and made tail certification unreachable — a real
        # regression for the background-panel workflow. The guard ran fine; only
        # the tree moved, which freshness already covers.
        d, tf = self._proj()
        before = R._snapshot_repo_state(d, tf)
        (d / "note.md").write_text("x\n", encoding="utf-8")
        _commit_all(d, "concurrent commit")
        with mock.patch("provider.sandbox.containment_available", return_value=True):
            full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(full["cautions"])
        self.assertFalse(full["degraded"], "head movement is not a degraded GUARD")

    def test_a_real_guard_failure_does_mark_the_round_degraded(self):
        d, tf = self._proj()
        before = {"porcelain": "", "task_hash": None, "dirty_hashes": {},
                  "z_read_ok": False, "is_git": True}
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(full["degraded"], full)

    def test_record_exemption_uses_exact_known_names(self):
        # opus F3 / sonnet #1: `judge-[^/"]*\.log` exempted ANY novel sibling.
        d = _repo()
        td = d / ".agent" / "tasks" / "001-x"
        td.mkdir(parents=True)
        tf = td / "task.md"
        tf.write_text("gate\n", encoding="utf-8")
        _commit_all(d)
        before = R._snapshot_repo_state(d, tf)
        (td / "judge.log").write_text("real claude log\n", encoding="utf-8")       # a REAL name
        (td / "judge-evil.log").write_text("planted\n", encoding="utf-8")          # not a known name
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("judge-evil.log" in m for m in full["mutations"]), full)
        self.assertFalse(any("judge.log" in m and "evil" not in m for m in full["mutations"]), full)


class RoundStampPreSpawn(unittest.TestCase):
    """Task 085 R2 (083 W10, sol-high #2): on a contained host a concurrent commit
    during a review is only a CAUTION, and the panel then fingerprinted the
    POST-review tree and stamped it as reviewed — the close read FRESH for code the
    judges never saw. The stamp must be the tree as it was when the judges were
    spawned; if it moved, the round records the pre-spawn value (so the close reads
    STALE) and carries no tail-cert descriptor."""

    def _proj(self):
        d = _repo()
        (d / "a.py").write_text("a = 1\n", encoding="utf-8")
        _git("add", "-A", cwd=d)
        _git("commit", "-qm", "seed", cwd=d)
        return d

    def test_a_commit_during_the_review_is_not_stamped_as_reviewed(self):
        from tasks.core import tree_state_fingerprint
        d = self._proj()
        before = tree_state_fingerprint(d)                 # judges spawned here
        (d / "a.py").write_text("a = 2  # landed while the judges ran\n", encoding="utf-8")
        _git("commit", "-qam", "concurrent commit", cwd=d)
        stamp, note = R._round_tree_stamp(d, before)
        self.assertEqual(note, "moved", "the concurrent commit was not noticed")
        self.assertEqual(stamp, before, "the post-review tree was stamped as reviewed")
        self.assertNotEqual(stamp, tree_state_fingerprint(d))

    def test_an_unchanged_tree_is_stamped_as_before(self):
        from tasks.core import tree_state_fingerprint
        d = self._proj()
        before = tree_state_fingerprint(d)
        stamp, note = R._round_tree_stamp(d, before)
        self.assertEqual(note, "")
        self.assertEqual(stamp, before)

    def test_no_pre_spawn_fingerprint_is_never_replaced_by_the_post_review_one(self):
        # Task 085 round 2, T5 (impl panel sol-high #2): git failing at spawn time
        # used to fall back to the POST-review fingerprint with moved=False — a
        # fresh-looking stamp for a tree no judge was shown.
        d = self._proj()
        stamp, note = R._round_tree_stamp(d, "")
        self.assertEqual((stamp, note), ("", "no-before"))

    def test_an_unreadable_tree_after_the_review_carries_no_descriptor(self):
        from tasks.core import tree_state_fingerprint
        d = self._proj()
        before = tree_state_fingerprint(d)
        with mock.patch("tasks.core.tree_state_fingerprint", return_value=""):
            stamp, note = R._round_tree_stamp(d, before)
        self.assertEqual((stamp, note), (before, "no-after"))

    def test_the_panel_fingerprints_before_spawning_and_uses_it(self):
        import inspect
        src = inspect.getsource(R.cmd_panel_review)
        self.assertIn("_fp_before = tree_state_fingerprint(project_path)", src)
        self.assertLess(src.index("_fp_before = tree_state_fingerprint(project_path)"),
                        src.index("executor.submit(run_judge"),
                        "the pre-spawn fingerprint must be taken before the judges run")
        self.assertIn("_round_tree_stamp(project_path, _fp_before)", src)


class PanelStampEndToEnd(unittest.TestCase):
    """Task 085 round 2, T4/T5/T6: the round header of a REAL `cmd_panel_review`
    run (judges faked at the adapter; the tamper detector stubbed clean so the
    host's containment does not decide the outcome). R2's first proof was only a
    source-order check (impl panel opus #2)."""

    def setUp(self):
        from tasks.core import tree_state_fingerprint
        self.tsf = tree_state_fingerprint
        self.d = _repo()
        (self.d / "a.py").write_text("a = 1\n", encoding="utf-8")
        _git("add", "-A", cwd=self.d)
        _git("commit", "-qm", "seed", cwd=self.d)
        self.tdir = self.d / ".agent" / "tasks" / "042-demo"
        self.tdir.mkdir(parents=True)
        (self.tdir / "task.md").write_text(
            "# 042 - demo\n## Status\npending\n## Intent\nx\n"
            "## Work Plan\n- [ ] a gate\n", encoding="utf-8")
        (self.d / ".agent" / "models.json").write_text(
            '{"panel": ["claude:opus", "claude:sonnet"], "default_judge": "claude:opus"}',
            encoding="utf-8")
        R._PB_JOURNAL_MOD = None
        R._PB_JOURNAL_LOADED = False

    def _panel(self, judge):
        import contextlib
        from provider.adapters.claude import ClaudeAdapter
        old = os.getcwd()
        patches = [
            mock.patch.object(ClaudeAdapter, "is_available", classmethod(lambda cls: True)),
            mock.patch.object(ClaudeAdapter, "run_headless_judge", judge),
            mock.patch.object(R, "_detect_tamper_full",
                              lambda pp, t, b: {"mutations": [], "cautions": [], "degraded": False}),
        ]
        for p in patches:
            p.start()
        os.chdir(self.d)
        try:
            # utf-8 sink: the CLI reconfigures its streams to utf-8, and this
            # in-process call bypasses the CLI — a cp1252 default (Windows lane)
            # raised on the round's `⚠` warnings (CI run 35881113729).
            with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.suppress(SystemExit), \
                 contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
                R.cmd_panel_review(["042", "--mode", "impl",
                                    "--models", "claude:opus,claude:sonnet"])
        finally:
            os.chdir(old)
            for p in reversed(patches):
                p.stop()
        return (self.tdir / "judge.md").read_text(encoding="utf-8")

    def _head(self):
        return _git("rev-parse", "HEAD", cwd=self.d).stdout.strip()

    def test_a_commit_during_a_real_panel_run_stamps_the_pre_review_tree(self):
        import threading
        fp0, head0 = self.tsf(self.d), self._head()
        once = threading.Lock()
        done = []

        def judge(adapter, **kw):
            with once:
                if not done:
                    (self.d / "a.py").write_text("a = 2  # landed mid-review\n", encoding="utf-8")
                    _git("commit", "-qam", "concurrent", cwd=self.d)
                    done.append(1)
            return "1. **Note** — fine.\n"

        text = self._panel(judge)
        self.assertNotEqual(self.tsf(self.d), fp0, "the fixture did not move the tree")
        self.assertIn(f"**Tree-state:** {fp0}", text)
        self.assertIn("**Tree moved during review:** yes", text)
        self.assertNotIn("**Panel-snapshot:**", text)
        self.assertIn(f"**Commit:** {head0}", text,
                      "the post-review HEAD was recorded as the reviewed commit")

    def test_an_undisturbed_panel_run_stamps_and_describes_the_tree(self):
        fp0, head0 = self.tsf(self.d), self._head()
        text = self._panel(lambda adapter, **kw: "1. **Note** — fine.\n")
        self.assertIn(f"**Tree-state:** {fp0}", text)
        self.assertIn("**Panel-snapshot:**", text)
        self.assertIn(f"**Commit:** {head0}", text)
        self.assertNotIn("Tree moved during review", text)

    def test_no_pre_spawn_fingerprint_emits_no_stamp(self):
        real, calls = self.tsf, []

        def flaky(project_path, **kw):
            calls.append(1)
            return "" if len(calls) == 1 else real(project_path, **kw)

        with mock.patch("tasks.core.tree_state_fingerprint", flaky):
            text = self._panel(lambda adapter, **kw: "1. **Note** — fine.\n")
        self.assertNotIn("**Tree-state:**", text)
        self.assertNotIn("**Panel-snapshot:**", text)
        self.assertIn("**Tree-state unavailable:**", text)


if __name__ == "__main__":
    unittest.main()
