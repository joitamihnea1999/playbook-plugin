#!/usr/bin/env python3
"""Executable pre-panel audit + its negative controls (report P6).

The audit's whole value depends on a discipline the rest of the report insists on
(Part 4): a measuring tool must PROVE it can report failure, or a green reading is
just a more elaborate hallucination. So every default sweep here is exercised
twice — against a CLEAN fixture (must stay quiet) and a DIRTY fixture containing
its exact target (must fire). A sweep that can't detect its own target would be a
false-green machine.

Also pinned: exit ≥2 is ERROR (did not run), never a pass; error-severity findings
fail the audit while advisory ones don't; config sweeps merge with the defaults.

Run: python3 tests/test_audit.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))
from tasks.audit import (  # noqa: E402
    DEFAULT_SWEEPS, audit_freshness_note, check_mindmap_staleness, classify,
    format_audit_receipt, resolve_sweeps, run_audit, run_sweep,
    _extract_mindmap_paths,
)


def sweep_by_name(name):
    return next(s for s in DEFAULT_SWEEPS if s["name"] == name)


class Classify(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(classify(0), "findings")
        self.assertEqual(classify(1), "clean")
        self.assertEqual(classify(2), "error")   # did NOT run — not a pass
        self.assertEqual(classify(127), "error")


class NegativeControls(unittest.TestCase):
    """Each default sweep: quiet on clean, fires on dirty."""

    def _proj(self):
        d = Path(tempfile.mkdtemp())
        (d / "src").mkdir()
        (d / "src" / "ok.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        return d

    def _run(self, name, project):
        return run_sweep(sweep_by_name(name), project)["status"]

    def test_conflict_markers(self):
        p = self._proj()
        self.assertEqual(self._run("conflict-markers", p), "clean")
        (p / "src" / "bad.py").write_text(
            "a = 1\n<<<<<<< HEAD\nb = 2\n>>>>>>> feature\n", encoding="utf-8")
        self.assertEqual(self._run("conflict-markers", p), "findings")

    def test_conflict_markers_ignores_markdown_underline(self):
        # A 7-'=' markdown underline must NOT read as a conflict marker.
        p = self._proj()
        (p / "README.md").write_text("Title\n=======\n\nbody\n", encoding="utf-8")
        self.assertEqual(self._run("conflict-markers", p), "clean")

    def test_merge_artifacts(self):
        p = self._proj()
        self.assertEqual(self._run("merge-artifacts", p), "clean")
        (p / "src" / "ok.py.orig").write_text("old\n", encoding="utf-8")
        self.assertEqual(self._run("merge-artifacts", p), "findings")

    def test_stale_markers(self):
        p = self._proj()
        self.assertEqual(self._run("stale-markers", p), "clean")
        (p / "src" / "todo.py").write_text("# TODO: fix this\n", encoding="utf-8")
        self.assertEqual(self._run("stale-markers", p), "findings")

    def test_agent_dir_is_excluded(self):
        # A `- [ ]` or TODO inside .agent/ (task files) must never be a finding.
        p = self._proj()
        (p / ".agent").mkdir()
        (p / ".agent" / "task.md").write_text("- [ ] TODO: a real gate\n", encoding="utf-8")
        self.assertEqual(self._run("stale-markers", p), "clean")


class ErrorIsNotAPass(unittest.TestCase):
    def test_a_broken_sweep_is_error_not_clean(self):
        p = Path(tempfile.mkdtemp())
        broken = {"name": "broken", "severity": "error", "why": "x", "command": "exit 2"}
        self.assertEqual(run_sweep(broken, p)["status"], "error")

    def test_error_fails_the_audit(self):
        p = Path(tempfile.mkdtemp())
        (p / ".agent").mkdir()
        (p / ".agent" / "config.json").write_text(json.dumps({
            "audit": {"disable_defaults": True,
                      "sweeps": [{"name": "b", "command": "exit 3", "severity": "advisory"}]}
        }))
        self.assertFalse(run_audit(p)["passed"])   # even an ADVISORY sweep erroring fails


class Timeout(unittest.TestCase):
    """A hung sweep must not hang the audit, and a killed scan is ERROR — an
    incomplete scan can certify nothing (A1)."""

    def test_hanging_sweep_is_error_not_pass(self):
        p = Path(tempfile.mkdtemp())
        (p / ".agent").mkdir()
        (p / ".agent" / "config.json").write_text(json.dumps({
            "audit": {"disable_defaults": True, "timeout_secs": 1,
                      "sweeps": [{"name": "hang", "command": "sleep 5",
                                  "severity": "advisory"}]}}))
        a = run_audit(p)
        self.assertFalse(a["passed"])
        (r,) = a["results"]
        self.assertEqual(r["status"], "error")
        self.assertIn("timed out", r["output"])


class Severity(unittest.TestCase):
    def _proj_with(self, sweeps):
        p = Path(tempfile.mkdtemp())
        (p / ".agent").mkdir()
        (p / ".agent" / "config.json").write_text(json.dumps(
            {"audit": {"disable_defaults": True, "sweeps": sweeps}}))
        return p

    def test_advisory_findings_do_not_fail(self):
        p = self._proj_with([{"name": "a", "command": "echo hit", "severity": "advisory"}])
        # echo → exit 0 → findings, but advisory → audit still passes
        self.assertTrue(run_audit(p)["passed"])

    def test_error_findings_fail(self):
        p = self._proj_with([{"name": "a", "command": "echo hit", "severity": "error"}])
        self.assertFalse(run_audit(p)["passed"])

    def test_clean_passes(self):
        p = self._proj_with([{"name": "a", "command": "exit 1", "severity": "error"}])
        self.assertTrue(run_audit(p)["passed"])


class NoUsableBashFailsClosed(unittest.TestCase):
    """When bash cannot run the scan, the audit must ERROR, never certify clean.

    On Windows a bare `bash` on PATH is the System32 WSL launcher; with no distro
    it exits non-zero, which classify() would read as exit 1 = "clean" — a
    false-green that never scanned. Simulated here on any host by pointing
    $PLAYBOOK_VERIFY_BASH at a stub that behaves like the WSL launcher. Red
    against the pre-fix code that invoked a bare `bash` and ignored the resolver.
    """

    def setUp(self):
        # The resolver's per-process cache now lives in the shared resolver
        # (tasks/bash_resolver.py), which audit.py delegates to; reset THAT to
        # force re-resolution against the stub.
        import tasks.bash_resolver as resolver_mod
        self.resolver = resolver_mod
        self._real_env = os.environ.get("PLAYBOOK_VERIFY_BASH")
        self._real_cache = resolver_mod._RESOLVED_BASH
        self.tmp = Path(tempfile.mkdtemp())
        stub = self.tmp / "wsl-stub.sh"
        # Mimic the WSL launcher: print an install hint, exit non-zero, and
        # crucially NEVER run the script it was handed.
        stub.write_text(
            "#!/bin/sh\n"
            "echo 'Windows Subsystem for Linux has no installed distributions.' >&2\n"
            "exit 1\n", encoding="utf-8")
        stub.chmod(0o755)
        os.environ["PLAYBOOK_VERIFY_BASH"] = str(stub)
        resolver_mod._RESOLVED_BASH = None  # force re-resolution against the stub

        def _restore():
            resolver_mod._RESOLVED_BASH = self._real_cache
            if self._real_env is None:
                os.environ.pop("PLAYBOOK_VERIFY_BASH", None)
            else:
                os.environ["PLAYBOOK_VERIFY_BASH"] = self._real_env
        self.addCleanup(_restore)

    def test_sweep_that_would_find_dirt_errors_when_bash_is_unusable(self):
        p = Path(tempfile.mkdtemp())
        (p / "src").mkdir()
        (p / "src" / "bad.py").write_text(
            "a = 1\n<<<<<<< HEAD\nb = 2\n>>>>>>> feature\n", encoding="utf-8")
        r = run_sweep(sweep_by_name("conflict-markers"), p)
        self.assertEqual(r["status"], "error",
                         "an unrun scan was certified as something other than error")
        self.assertNotEqual(r["status"], "clean")

    def test_audit_does_not_pass_when_no_bash(self):
        p = Path(tempfile.mkdtemp())
        (p / ".agent").mkdir()
        (p / ".agent" / "config.json").write_text(json.dumps(
            {"audit": {"disable_defaults": True,
                       "sweeps": [{"name": "a", "command": "exit 1",
                                   "severity": "advisory"}]}}))
        self.assertFalse(run_audit(p)["passed"],
                         "audit passed though no sweep could actually run")


class ResolveSweeps(unittest.TestCase):
    def test_defaults_when_no_config(self):
        p = Path(tempfile.mkdtemp())
        self.assertEqual([s["name"] for s in resolve_sweeps(p)],
                         [s["name"] for s in DEFAULT_SWEEPS])

    def test_project_sweeps_append_to_defaults(self):
        p = Path(tempfile.mkdtemp())
        (p / ".agent").mkdir()
        (p / ".agent" / "config.json").write_text(json.dumps(
            {"audit": {"sweeps": [{"name": "mine", "command": "grep -r X ."}]}}))
        names = [s["name"] for s in resolve_sweeps(p)]
        self.assertIn("conflict-markers", names)
        self.assertIn("mine", names)

    def test_malformed_sweep_skipped(self):
        p = Path(tempfile.mkdtemp())
        (p / ".agent").mkdir()
        (p / ".agent" / "config.json").write_text(json.dumps(
            {"audit": {"disable_defaults": True, "sweeps": [{"name": "no-command"}]}}))
        self.assertEqual(resolve_sweeps(p), [])


class MindmapStaleness(unittest.TestCase):
    """A mind-map citing a file that no longer exists is stale (report P6). High
    precision: moved files, URLs, function names, and commit hashes must NOT fire."""

    def _proj(self):
        d = Path(tempfile.mkdtemp())
        (d / "tasks").mkdir()
        (d / "tasks" / "router.py").write_text("x = 1\n", encoding="utf-8")
        return d

    def _map(self, proj, body):
        (proj / "MIND_MAP.md").write_text(body, encoding="utf-8")

    def test_no_map_is_omitted(self):
        self.assertIsNone(check_mindmap_staleness(Path(tempfile.mkdtemp())))

    def test_clean_when_cited_file_exists(self):
        p = self._proj()
        self._map(p, "[1] **Router** - routing lives in `tasks/router.py`.\n")
        self.assertEqual(check_mindmap_staleness(p)["status"], "clean")

    def test_fires_on_a_deleted_file(self):
        p = self._proj()
        self._map(p, "[1] **Ghost** - handled in `tasks/ghost_module.py` (gone).\n")
        r = check_mindmap_staleness(p)
        self.assertEqual(r["status"], "findings")
        self.assertIn("tasks/ghost_module.py", r["output"])

    def test_moved_file_not_flagged(self):
        # Cited under an old dir, but the basename still exists → not stale.
        p = self._proj()
        self._map(p, "[1] **Router** - see `old/legacy/router.py`.\n")
        self.assertEqual(check_mindmap_staleness(p)["status"], "clean")

    def test_url_not_flagged(self):
        p = self._proj()
        self._map(p, "[1] history: https://github.com/o/repo/blob/main/tasks/deleted.py\n")
        self.assertEqual(check_mindmap_staleness(p)["status"], "clean")

    def test_function_names_and_hashes_not_flagged(self):
        p = self._proj()
        self._map(p, "[1] `_node_starts` at commit a1b2c3d handles fences; 25000 chars.\n")
        self.assertEqual(check_mindmap_staleness(p)["status"], "clean")

    def test_extension_not_matched_as_substring(self):
        """Field FP (StrataDB batch 2): `config.json` was extracted as
        `config.js` — `js` tried before `json`, no boundary guard."""
        paths = _extract_mindmap_paths("declared in `pkg/config.json` at startup")
        self.assertIn("pkg/config.json", paths)
        self.assertNotIn("pkg/config.js", paths)

    def test_placeholder_paths_not_flagged(self):
        """Field FP: `journal/NNN.md` documents a NAMING SCHEME, not a file."""
        p = self._proj()
        (p / "journal").mkdir()
        self._map(p, "[1] one entry per task: `journal/NNN.md` (NNN = task number).\n")
        self.assertEqual(check_mindmap_staleness(p)["status"], "clean")

    def test_dot_dir_citations_handled(self):
        """Field FP #3: `.agent/config.json` was lstrip-mangled to
        `agent/config.json` AND lives in a walker-excluded dir — citations into
        excluded dirs are unjudgeable and must be skipped, not flagged."""
        p = self._proj()
        (p / ".agent").mkdir()
        (p / ".agent" / "config.json").write_text("{}", encoding="utf-8")
        self._map(p, "[1] declared in `.agent/config.json`; see `.claude/x.py` too.\n")
        r = check_mindmap_staleness(p)
        self.assertNotIn("agent/config.json", r["output"])
        # .claude/ IS walked, so a missing .claude/x.py is a legitimate finding.
        self.assertIn(".claude/x.py", r["output"])

    def test_str_project_path_accepted(self):
        """Field crash: a str project_path hit `str / str` in load_config."""
        p = self._proj()
        self._map(p, "[1] router in `tasks/router.py`.\n")
        r = check_mindmap_staleness(str(p))   # str, not Path — must not raise
        self.assertEqual(r["status"], "clean")

    def test_extractor_precision(self):
        paths = _extract_mindmap_paths(
            "see `tasks/router.py:42` and utils/helpers.js but not input/output "
            "nor foo.com/bar.py nor bare.py")
        self.assertIn("tasks/router.py", paths)
        self.assertIn("utils/helpers.js", paths)
        self.assertNotIn("foo.com/bar.py", paths)   # domain-shaped
        self.assertNotIn("bare.py", paths)           # no directory

    def test_participates_in_run_audit(self):
        p = self._proj()
        self._map(p, "[1] gone: `tasks/nope.py`\n")
        names = [r["name"] for r in run_audit(p)["results"]]
        self.assertIn("mindmap-stale-refs", names)

    def test_severity_configurable_to_error(self):
        p = self._proj()
        self._map(p, "[1] gone: `tasks/nope.py`\n")
        # advisory default → audit still passes
        self.assertTrue(run_audit(p)["passed"])
        (p / ".agent").mkdir()
        (p / ".agent" / "config.json").write_text(json.dumps(
            {"audit": {"mindmap_severity": "error"}}))
        # zero-tolerance → stale mind-map now fails the audit
        self.assertFalse(run_audit(p)["passed"])


class TaskBloat(unittest.TestCase):
    """An open task.md past the review budget gets judged through a keyhole —
    the sweep nudges the sanctioned compaction BEFORE that happens (1.5.3)."""

    def _proj(self, body):
        d = Path(tempfile.mkdtemp())
        td = d / ".agent" / "tasks" / "001-t"
        td.mkdir(parents=True)
        (td / "task.md").write_text(body, encoding="utf-8")
        return d

    def test_open_oversized_task_flagged(self):
        from tasks.audit import check_task_bloat
        r = check_task_bloat(self._proj("## Status\npending\n" + "x" * 60_000))
        self.assertEqual(r["status"], "findings")
        self.assertIn("001-t/task.md", r["output"])
        self.assertIn("compact", r["output"])

    def test_small_open_task_clean(self):
        from tasks.audit import check_task_bloat
        r = check_task_bloat(self._proj("## Status\npending\nsmall\n"))
        self.assertEqual(r["status"], "clean")

    def test_done_tasks_ignored(self):
        from tasks.audit import check_task_bloat
        # A closed task may be huge — it is no longer being reviewed.
        r = check_task_bloat(self._proj("## Status\ndone\n" + "x" * 60_000))
        self.assertIsNone(r, "no open tasks → nothing to measure")

    def test_threshold_configurable(self):
        from tasks.audit import check_task_bloat
        p = self._proj("## Status\npending\n" + "x" * 5_000)
        (p / ".agent" / "config.json").write_text(
            json.dumps({"audit": {"task_bloat_chars": 1_000}}))
        self.assertEqual(check_task_bloat(p)["status"], "findings")

    def test_participates_in_run_audit(self):
        p = self._proj("## Status\npending\n" + "x" * 60_000)
        names = [r["name"] for r in run_audit(p)["results"]]
        self.assertIn("task-bloat", names)
        # advisory: an oversized task must not FAIL the audit
        self.assertTrue(run_audit(p)["passed"])


class Receipt(unittest.TestCase):
    def test_records_verdict_and_findings(self):
        audit = {"passed": False, "results": [
            {"name": "conflict-markers", "severity": "error", "why": "broken code",
             "status": "findings", "output": "src/bad.py:2:<<<<<<< HEAD\n", "rc": 0, "command": "x"},
            {"name": "stale-markers", "severity": "advisory", "why": "todos",
             "status": "clean", "output": "", "rc": 1, "command": "y"},
        ]}
        # Entry form: the heading belongs to core.upsert_task_section; the entry
        # leads with `### ts · VERDICT · commit sha` (the sha is what freshness
        # checks parse).
        r = format_audit_receipt(audit, timestamp="2026-08-11T10:00:00", head_sha="abc1234")
        self.assertTrue(r.startswith("### 2026-08-11T10:00:00 · FAIL · commit abc1234"), r)
        self.assertNotIn("## Pre-Panel Audit", r)
        self.assertIn("[FINDINGS(1)] conflict-markers", r)
        self.assertIn("[CLEAN] stale-markers", r)


class Freshness(unittest.TestCase):
    """'An audit ran once' is not freshness (A4): the newest receipt's commit
    must match HEAD or the nudge fires."""

    HEAD = "a" * 40

    def _task_text(self, sha):
        return ("# 1 - t\n\n## Pre-Panel Audit\n\n"
                f"### 2026-08-11T10:00:00 · PASS · commit {sha}\n"
                "    - [CLEAN] conflict-markers — broken code\n")

    def test_no_receipt_nudges(self):
        note = audit_freshness_note("# 1 - t\n\n## Status\npending\n", self.HEAD)
        self.assertIsNotNone(note)
        self.assertIn("no pre-panel audit", note)

    def test_stale_receipt_nudges(self):
        note = audit_freshness_note(self._task_text("b" * 40), self.HEAD)
        self.assertIsNotNone(note)
        self.assertIn("STALE", note)

    def test_fresh_receipt_is_quiet(self):
        self.assertIsNone(audit_freshness_note(self._task_text(self.HEAD), self.HEAD))

    def test_unknown_commit_in_receipt_nudges(self):
        text = ("## Pre-Panel Audit\n\n### t · PASS · commit (unknown)\n")
        self.assertIn("STALE", audit_freshness_note(text, self.HEAD))

    def test_no_git_head_stays_quiet(self):
        self.assertIsNone(audit_freshness_note(self._task_text("b" * 40), ""))

    def test_newest_entry_wins(self):
        # upsert inserts newest FIRST — a fresh entry above a stale one is fresh.
        text = ("## Pre-Panel Audit\n\n"
                f"### new · PASS · commit {self.HEAD}\n"
                "### old · FAIL · commit " + "b" * 40 + "\n")
        self.assertIsNone(audit_freshness_note(text, self.HEAD))


if __name__ == "__main__":
    unittest.main()
