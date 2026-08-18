#!/usr/bin/env python3
"""Judge isolation + tamper guard (task 018 / bug report #1).

Judges are read-only evaluators. Two independent defenses, both tested here:
  1. OS containment — every judge spawn passes `project_writable=False` to
     `provider.sandbox.run`, so seatbelt/bwrap deny project writes.
  2. Tamper guard — panel & single-judge paths snapshot the repo before spawning
     and hard-stop (non-zero, loud banner, judge.md still saved) if the working
     tree changed, the ONLY defense on uncontained platforms (Windows/nested).

(1) is covered for the five panel adapters (direct call) and the five inline
single-judge cli.py arms (in-process `main()` drive). (2) is covered against the
helpers directly.

Pure stdlib unittest. Run: python3 tests/test_judge_isolation.py
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(_PLUGIN))

from provider.adapters.claude import ClaudeAdapter  # noqa: E402
from provider.adapters.antigravity import AntigravityAdapter  # noqa: E402
from provider.adapters.codex import CodexAdapter  # noqa: E402
from provider.adapters.grok import GrokAdapter  # noqa: E402
from provider.adapters.pi import PiAdapter  # noqa: E402
from tasks import cli as tcli  # noqa: E402
from tasks import review as treview  # noqa: E402  (tamper trio moved by the 1.5.9 split)


def _ok_result(stdout="REVIEW BODY"):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class AdapterContainmentTest(unittest.TestCase):
    """Each panel adapter's run_headless_judge must spawn read-only."""

    ADAPTERS = [
        (ClaudeAdapter, "claude"),
        (AntigravityAdapter, "agy"),
        (CodexAdapter, "codex"),
        (GrokAdapter, "grok"),
        (PiAdapter, "pi"),
    ]

    def _run_and_capture(self, adapter_cls, binary):
        captured = {}

        def fake_run(agent, agent_args, **kwargs):
            captured["agent"] = agent
            captured["kwargs"] = kwargs
            return _ok_result()

        a = adapter_cls(session_id="judge", project_root=Path("/tmp/proj"))
        with mock.patch("shutil.which", return_value=f"/usr/bin/{binary}"), \
             mock.patch("provider.sandbox.run", side_effect=fake_run), \
             mock.patch("provider.sandbox.format_judge_output", side_effect=lambda r: r.stdout):
            a.run_headless_judge(
                prompt="review this", model=None, system_context="CTX",
                web_search=False, timeout_secs=60, budget_usd="10",
            )
        return captured

    def test_all_adapters_pass_project_writable_false(self):
        for adapter_cls, binary in self.ADAPTERS:
            with self.subTest(adapter=binary):
                cap = self._run_and_capture(adapter_cls, binary)
                self.assertIn("kwargs", cap, f"{binary}: sandbox.run was never called")
                self.assertIs(
                    cap["kwargs"].get("project_writable"), False,
                    f"{binary} judge spawned WITHOUT project_writable=False — "
                    "a rogue judge could write the repo",
                )


class InlineSingleJudgeContainmentTest(unittest.TestCase):
    """The five inline single-judge cli.py arms bypass the adapters; each must
    also spawn read-only. Driven through main() so the real dispatch runs."""

    def _drive(self, backend, binary):
        proj = Path(self.tmp)
        captured = {}

        def fake_run(agent, agent_args, **kwargs):
            captured["agent"] = agent
            captured["kwargs"] = kwargs
            return _ok_result()

        argv = ["tasks", "plan-review", "001", "--backend", backend]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch("shutil.which", return_value=f"/usr/bin/{binary}"), \
             mock.patch("provider.sandbox.run", side_effect=fake_run), \
             mock.patch("provider.sandbox.format_judge_output", side_effect=lambda r: r.stdout):
            cwd = os.getcwd()
            os.chdir(proj)
            try:
                tcli.main()
            except SystemExit:
                pass
            finally:
                os.chdir(cwd)
        return captured

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        proj = Path(self.tmp)
        tdir = proj / ".agent" / "tasks" / "001-test"
        tdir.mkdir(parents=True)
        (tdir / "task.md").write_text(
            "# 001 - Test\n\n## Status\npending\n\n## Intent\nx\n\n"
            "## Design Phase\n- [ ] a gate\n", encoding="utf-8")
        (proj / "MIND_MAP.md").write_text("# Mind Map\n[1] node\n", encoding="utf-8")

    def tearDown(self):
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def test_inline_arms_pass_project_writable_false(self):
        for backend, binary in [("claude", "claude"), ("codex", "codex"),
                                ("antigravity", "agy"), ("grok", "grok"),
                                ("pi", "pi")]:
            with self.subTest(backend=backend):
                cap = self._drive(backend, binary)
                self.assertIn("kwargs", cap,
                              f"{backend}: sandbox.run never called (dispatch changed?)")
                self.assertIs(
                    cap["kwargs"].get("project_writable"), False,
                    f"inline {backend} arm spawned WITHOUT project_writable=False",
                )


class TamperGuardTest(unittest.TestCase):
    def _git_repo(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.email", "x@y.z"], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.name", "x"], check=True)
        return d

    def test_clean_run_no_tamper(self):
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        self.assertEqual(treview._detect_tamper(d, tf, before), [])

    def test_monitor_state_churn_is_not_tamper(self):
        # F22 (batch-7 wake 3): the conversation monitor writes trace.md /
        # session.md in .agent/monitor/ WHILE a panel runs — a sanctioned
        # concurrent writer, OS-contained to exactly that dir. Its churn cost
        # the agent a verification cycle when the tamper banner named it.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        mon = d / ".agent" / "monitor"
        mon.mkdir(parents=True)
        (mon / "trace.md").write_text("wake 1\n")
        lane_mon = d / ".agent" / "alice" / "monitor"
        lane_mon.mkdir(parents=True)
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        (mon / "trace.md").write_text("wake 1\nwake 2\n")     # tracked churn
        (mon / "session.md").write_text("judgment\n")          # new file
        (lane_mon / "trace.md").write_text("lane wake\n")      # per-user lane
        self.assertEqual(treview._detect_tamper(d, tf, before), [],
                         "monitor state churn must not read as judge tampering")

    def test_non_monitor_agent_file_still_flags(self):
        # Negative control: the exclusion is the monitor dir ONLY — a new file
        # elsewhere under .agent (or anywhere) still trips the guard.
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        (d / ".agent").mkdir()
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        (d / ".agent" / "monitor-notes.md").write_text("rogue\n")
        changes = treview._detect_tamper(d, tf, before)
        self.assertTrue(changes, "a non-monitor .agent file must still flag")

    def test_git_tamper_catches_taskmd_edit_and_new_file(self):
        d = self._git_repo()
        tf = d / "task.md"
        tf.write_text("gate1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
        before = treview._snapshot_repo_state(d, tf)
        tf.write_text("gate1 REWRITTEN BY ROGUE\n")   # task.md rewrite
        (d / "task_audit.md").write_text("fabricated\n")  # new rogue file
        changes = treview._detect_tamper(d, tf, before)
        joined = " ".join(changes)
        self.assertIn("task.md", joined)
        self.assertIn("task_audit.md", joined)

    def test_non_git_repo_falls_back_to_taskmd_hash(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        tf = d / "task.md"
        tf.write_text("a\n")
        before = treview._snapshot_repo_state(d, tf)
        self.assertIsNone(before["porcelain"])          # not a git repo
        self.assertIsNotNone(before["task_hash"])
        self.assertEqual(treview._detect_tamper(d, tf, before), [])   # unchanged
        tf.write_text("b\n")
        self.assertTrue(treview._detect_tamper(d, tf, before))        # changed

    def test_banner_names_changes_and_says_do_not_ingest(self):
        banner = treview._tamper_banner(["working tree: ?? rogue.md",
                                      "task.md content changed (task.md)"])
        self.assertIn("TAMPER DETECTED", banner)
        self.assertIn("rogue.md", banner)
        self.assertIn("Do NOT ingest", banner)

    def test_no_taskmd_and_non_git_yields_no_signal(self):
        # Promptless panel on a non-git dir with no task.md: nothing to compare,
        # detector must return [] (no false positive), not crash.
        import tempfile
        d = Path(tempfile.mkdtemp())
        before = treview._snapshot_repo_state(d, None)
        self.assertEqual(treview._detect_tamper(d, None, before), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
