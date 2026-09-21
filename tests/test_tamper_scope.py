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
        import shutil
        shutil.rmtree(nested / ".git")
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
        import shutil
        shutil.rmtree(nested)
        try:
            os.symlink(a, nested, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted")
        before = R._snapshot_repo_state(proj, tf)
        os.unlink(nested)
        os.symlink(b, nested, target_is_directory=True)
        full = R._detect_tamper_full(proj, tf, before)
        self.assertTrue(any("nested" in m and "identity" in m for m in full["mutations"]), full)

    def test_head_moved_is_a_caution_not_a_mutation(self):
        proj, nested, tf = self._with_root()
        before = R._snapshot_repo_state(proj, tf)
        self.assertEqual(before["head"]["state"], "ok")
        (proj / "note.md").write_text("n\n", encoding="utf-8")
        _commit_all(proj, "concurrent")
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
        import shutil
        shutil.rmtree(td)
        try:
            os.symlink(elsewhere, td, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted")
        full = R._detect_tamper_full(d, tf, before)
        self.assertTrue(any("task directory identity" in m for m in full["mutations"]), full)


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
        import shutil
        shutil.rmtree(d / ".git")
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
        return {"mutations": [], "cautions": ["content-hash guard degraded: could not enumerate dirty files at close (`git status -z` failed)"]}

    def _mutated(self, *a, **k):
        return {"mutations": ["working tree: ?? rogue.md"], "cautions": []}


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
        (td / "judge-impl-codex.log").write_text("log\n", encoding="utf-8")
        (td / "judge-impl-claude.partial.log").write_text("p\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
