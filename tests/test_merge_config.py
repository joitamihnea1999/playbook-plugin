#!/usr/bin/env python3
"""Tests for the merge skill's project-declared verify command (task 021).

The merge skill used to hardcode one repo's layout and test runner (`backend/`
and `run-backend-tests`), which on any other project meant the verification was
a no-op or a hard error — and even on that repo it certified a merge "verified"
while another layer's suite was red. This covers the replacement:

  * `merge-verify.py` — the shipped runner: classification (declared / absent /
    unusable), exit-code contract, and quote-safe command transport.
  * doctor §1b — advisory `merge_verify` warnings, sharing the runner's rules.
  * merge-doctor — a tracked `.agent/config.json` is repo policy, not legacy
    detritus (without this the skill's own Step 7(b) gate fails on any repo that
    follows its instruction to commit the file).
  * a literal fence — the hardcoded names must not creep back into SKILL.md.

Pure stdlib unittest (honors the stdlib-only runtime invariant).
Run: python3 tests/test_merge_config.py   (or: python3 -m unittest ...)
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

# The runtime tree is plugins/playbook/ (dispatcher sets PYTHONPATH there).
_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(_PLUGIN))

from tasks.shared import _merge_verify_issues, _merge_verify_untracked  # noqa: E402
from tasks.core import SHARED_POLICY_PATHS, run_merge_doctor  # noqa: E402

_SKILL_DIR = _PLUGIN / "skills" / "merge"
_MERGE_VERIFY = _SKILL_DIR / "merge-verify.py"
_SKILL_MD = _SKILL_DIR / "SKILL.md"

# Exit-code contract — the push gate is "exit 0 and nothing else".
GREEN, FAILED, BLOCKED, SKIPPED, CONFIGURED = 0, 1, 2, 3, 4

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]

# The Step 7(d) recipe, pinned. Root-anchored pathspecs (`:/`, `,top`) so the
# scope can't narrow to the cwd; whole-tree minus the paths the semantic steps
# own, so it never diffs a directory that may not exist.
IDENTITY_DIFF_PATHSPECS = (
    "':/' ':(exclude,top)MIND_MAP.md' ':(exclude,top)MIND_MAP_OVERFLOW.md' "
    "':(exclude,top).agent'"
)
IDENTITY_DIFF = f'git diff "$target_before" -- {IDENTITY_DIFF_PATHSPECS}'


def _load_runner():
    spec = importlib.util.spec_from_file_location("_mv_under_test", _MERGE_VERIFY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mv = _load_runner()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [*_GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _write_config(root: Path, payload) -> None:
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (root / ".agent" / "config.json").write_text(text, encoding="utf-8")


def _run_cli(root: Path, *extra: str):
    """Invoke merge-verify.py as a subprocess — the way the skill invokes it."""
    proc = subprocess.run(
        [sys.executable, str(_MERGE_VERIFY), "-C", str(root), *extra],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


class TestClassification(unittest.TestCase):
    """Absent vs. declared vs. unusable — the asymmetry is the whole design."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_no_config_file_at_all_is_skipped(self):
        self.assertIsNone(mv.resolve_command(str(self.root)))

    def test_config_without_the_key_is_skipped(self):
        _write_config(self.root, {"judge_budget_usd": 2})
        self.assertIsNone(mv.resolve_command(str(self.root)))

    def test_empty_declaration_object_is_skipped(self):
        # `"merge_verify": {}` reads as a deliberate "declare nothing".
        _write_config(self.root, {"merge_verify": {}})
        self.assertIsNone(mv.resolve_command(str(self.root)))

    def test_blank_command_is_skipped(self):
        # The field report requires absent/empty to skip, not to run `bash ''`
        # and report a vacuous green.
        for blank in ("", "   ", "\t\n"):
            with self.subTest(blank=blank):
                _write_config(self.root, {"merge_verify": {"command": blank}})
                self.assertIsNone(mv.resolve_command(str(self.root)))

    def test_declared_command_is_returned(self):
        _write_config(self.root, {"merge_verify": {"command": "make test"}})
        self.assertEqual(mv.resolve_command(str(self.root)), "make test")

    def test_extra_keys_alongside_a_valid_command_are_tolerated(self):
        _write_config(self.root, {"merge_verify": {"command": "make test",
                                                   "timeout_secs": 900}})
        self.assertEqual(mv.resolve_command(str(self.root)), "make test")

    def test_present_but_unreadable_is_unusable_not_skipped(self):
        # A committed policy file that can't be read has still been declared;
        # calling that "nothing declared" is a false statement (impl panel, 7/9).
        _write_config(self.root, {"merge_verify": {"command": "make test"}})
        cfg = self.root / ".agent" / "config.json"
        cfg.chmod(0o000)
        self.addCleanup(cfg.chmod, 0o644)
        if os.access(cfg, os.R_OK):  # running as root — the mode is unenforced
            self.skipTest("cannot make a file unreadable as this user")
        with self.assertRaises(mv.Unusable) as ctx:
            mv.resolve_command(str(self.root))
        self.assertIn("cannot be read", str(ctx.exception))

    def test_directory_shaped_config_is_unusable_not_skipped(self):
        (self.root / ".agent" / "config.json").mkdir(parents=True)
        with self.assertRaises(mv.Unusable):
            mv.resolve_command(str(self.root))

    def test_missing_agent_dir_is_skipped(self):
        # NotADirectoryError path: .agent exists as a FILE, so there is no
        # config to speak of — genuinely absent, not broken.
        (self.root / ".agent").write_text("not a dir", encoding="utf-8")
        self.assertIsNone(mv.resolve_command(str(self.root)))

    def test_malformed_json_is_unusable_not_skipped(self):
        _write_config(self.root, '{"merge_verify": {"command": "x"},}')
        with self.assertRaises(mv.Unusable):
            mv.resolve_command(str(self.root))

    def test_non_object_toplevel_is_unusable(self):
        _write_config(self.root, "[1, 2, 3]")
        with self.assertRaises(mv.Unusable):
            mv.resolve_command(str(self.root))

    def test_string_declaration_is_unusable(self):
        _write_config(self.root, {"merge_verify": "make test"})
        with self.assertRaises(mv.Unusable):
            mv.resolve_command(str(self.root))

    def test_misspelled_command_key_is_unusable_not_skipped(self):
        # The dangerous case: silently skipping a typo would disable the gate
        # the project believes it declared.
        _write_config(self.root, {"merge_verify": {"commnd": "make test"}})
        with self.assertRaises(mv.Unusable) as ctx:
            mv.resolve_command(str(self.root))
        self.assertIn("commnd", str(ctx.exception))

    def test_non_string_command_is_unusable(self):
        for bad in (42, True, ["make", "test"], {"a": 1}, None):
            with self.subTest(bad=bad):
                _write_config(self.root, {"merge_verify": {"command": bad}})
                with self.assertRaises(mv.Unusable):
                    mv.resolve_command(str(self.root))


class TestExitCodes(unittest.TestCase):
    """Exit code IS the verdict: only 0 may clear push-gate 5."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_green_command_exits_zero(self):
        _write_config(self.root, {"merge_verify": {"command": "true"}})
        rc, out = _run_cli(self.root)
        self.assertEqual(rc, GREEN)
        self.assertIn("GREEN", out)

    def test_failing_command_exits_one_and_reports_its_own_rc(self):
        _write_config(self.root, {"merge_verify": {"command": "exit 7"}})
        rc, out = _run_cli(self.root)
        self.assertEqual(rc, FAILED)
        self.assertIn("FAILED", out)
        self.assertIn("7", out)

    def test_a_command_exiting_2_or_3_cannot_masquerade_as_blocked_or_skipped(self):
        # Distinct wrapper codes exist precisely so a command's own rc can't be
        # mistaken for a config verdict.
        for rc_in in (2, 3):
            with self.subTest(rc=rc_in):
                _write_config(self.root, {"merge_verify": {"command": f"exit {rc_in}"}})
                rc, out = _run_cli(self.root)
                self.assertEqual(rc, FAILED)
                self.assertIn("FAILED", out)

    def test_unconfigured_exits_skipped(self):
        rc, out = _run_cli(self.root)
        self.assertEqual(rc, SKIPPED)
        self.assertIn("SKIPPED", out)
        # Must say the check did not happen — silence is what let a red tree ride.
        self.assertIn("NOT checked", out)

    def test_unusable_exits_blocked(self):
        _write_config(self.root, {"merge_verify": {"command": 42}})
        rc, out = _run_cli(self.root)
        self.assertEqual(rc, BLOCKED)
        self.assertIn("BLOCKED", out)

    def test_plan_mode_never_runs_the_command(self):
        marker = self.root / "ran"
        _write_config(self.root, {"merge_verify": {"command": f"touch {marker}"}})
        rc, out = _run_cli(self.root, "--plan")
        self.assertIn("CONFIGURED", out)
        self.assertFalse(marker.exists(), "--plan must not execute the command")

    def test_plan_mode_does_not_return_the_push_gate_code(self):
        # --plan ran nothing, so it must not hand back the one code push-gate 5
        # accepts — otherwise a classification probe reads as a passing gate.
        _write_config(self.root, {"merge_verify": {"command": "exit 99"}})
        rc, _ = _run_cli(self.root, "--plan")
        self.assertEqual(rc, CONFIGURED)
        self.assertNotEqual(rc, GREEN)

    def test_early_failing_step_does_not_report_green(self):
        # bash reports only the LAST command's status: without `set -e` a red
        # suite followed by a successful line would pass the gate.
        for command in ("false\ntrue",
                        "echo start\nfalse\necho end",
                        "false | true"):
            with self.subTest(command=command):
                _write_config(self.root, {"merge_verify": {"command": command}})
                rc, out = _run_cli(self.root)
                self.assertEqual(rc, FAILED, f"{command!r} must not be GREEN:\n{out}")

    def test_failing_last_step_still_fails(self):
        _write_config(self.root, {"merge_verify": {"command": "true\nfalse"}})
        rc, _ = _run_cli(self.root)
        self.assertEqual(rc, FAILED)

    def test_plan_mode_reports_skipped_when_nothing_declared(self):
        rc, out = _run_cli(self.root, "--plan")
        self.assertEqual(rc, SKIPPED)


class TestNoUsableBashFailsClosed(unittest.TestCase):
    """When bash cannot run the command, the verify must NEVER report GREEN.

    On Windows a bare `bash` is the System32 WSL launcher; a stub exiting 0 would
    stamp a red tree GREEN, and one exiting 1 would never run the command yet
    read as FAILED. Simulated on any host with $PLAYBOOK_VERIFY_BASH pointing at
    a stub. Red against the pre-fix code that invoked a bare `bash`.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.stub = Path(self.tmp.name) / "wsl-stub.sh"
        # WSL-launcher shape: exit 0 WITHOUT running the handed script — the most
        # dangerous case, because a false 0 is the false-green.
        self.stub.write_text(
            "#!/bin/sh\necho 'no WSL distro' >&2\nexit 0\n", encoding="utf-8")
        self.stub.chmod(0o755)

    def _run_with_stub(self, root):
        env = {**os.environ, "PLAYBOOK_VERIFY_BASH": str(self.stub)}
        proc = subprocess.run(
            [sys.executable, str(_MERGE_VERIFY), "-C", str(root)],
            capture_output=True, text=True, env=env)
        return proc.returncode, proc.stdout + proc.stderr

    def test_a_would_be_green_command_does_not_report_green(self):
        _write_config(self.root, {"merge_verify": {"command": "true"}})
        rc, out = self._run_with_stub(self.root)
        self.assertNotEqual(rc, GREEN,
                            "a stub bash that never ran the command reported GREEN")
        self.assertEqual(rc, FAILED)
        self.assertIn("no usable bash", out)


class TestCommandTransport(unittest.TestCase):
    """A declared command is arbitrary project text; running it must not change
    its meaning. `bash -lc '<command>'` breaks on embedded single quotes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_single_quoted_command_survives(self):
        _write_config(self.root, {"merge_verify": {
            "command": """python3 -c 'print("ok")' && echo "it's fine" """}})
        rc, out = _run_cli(self.root)
        self.assertEqual(rc, GREEN)
        self.assertIn("ok", out)
        self.assertIn("it's fine", out)

    def test_multiline_command_survives(self):
        _write_config(self.root, {"merge_verify": {
            "command": "echo first\nif true; then echo second; fi"}})
        rc, out = _run_cli(self.root)
        self.assertEqual(rc, GREEN)
        self.assertIn("first", out)
        self.assertIn("second", out)

    def test_shell_metacharacters_survive(self):
        _write_config(self.root, {"merge_verify": {
            "command": 'echo "a|b;c&d $(echo sub)" && test 1 -lt 2'}})
        rc, out = _run_cli(self.root)
        self.assertEqual(rc, GREEN)
        self.assertIn("a|b;c&d sub", out)

    def test_command_runs_with_the_project_root_as_cwd(self):
        (self.root / "sentinel.txt").write_text("x", encoding="utf-8")
        _write_config(self.root, {"merge_verify": {"command": "test -f sentinel.txt"}})
        rc, _ = _run_cli(self.root, )
        self.assertEqual(rc, GREEN)

    def test_no_leftover_temp_script(self):
        _write_config(self.root, {"merge_verify": {"command": "true"}})
        before = set(Path(tempfile.gettempdir()).glob("merge-verify-*.sh"))
        _run_cli(self.root)
        after = set(Path(tempfile.gettempdir()).glob("merge-verify-*.sh"))
        self.assertEqual(before, after, "temp script must be cleaned up")


class TestDoctorAdvisory(unittest.TestCase):
    """doctor §1b warns, never fails — and shares the runner's rules."""

    def test_silent_when_nothing_declared(self):
        self.assertEqual(_merge_verify_issues({}), [])
        self.assertEqual(_merge_verify_issues({"judge_budget_usd": 2}), [])

    def test_silent_when_usable(self):
        self.assertEqual(_merge_verify_issues({"merge_verify": {"command": "make test"}}), [])

    def test_warns_when_declared_empty(self):
        issues = _merge_verify_issues({"merge_verify": {}})
        self.assertEqual(len(issues), 1)
        self.assertIn("SKIPPED", issues[0])

    def test_warns_with_block_consequence_when_unusable(self):
        for cfg in ({"merge_verify": "make test"},
                    {"merge_verify": {"commnd": "make test"}},
                    {"merge_verify": {"command": 42}}):
            with self.subTest(cfg=cfg):
                issues = _merge_verify_issues(cfg)
                self.assertEqual(len(issues), 1)
                # A doctor reader should learn the consequence, not just the shape.
                self.assertIn("BLOCK", issues[0])

    def test_non_dict_config_is_not_a_crash(self):
        for junk in (None, [], "text", 42):
            with self.subTest(junk=junk):
                self.assertEqual(_merge_verify_issues(junk), [])

    def test_a_corrupt_shipped_runner_does_not_crash_doctor(self):
        # The advisory loads merge-verify.py to reuse its rules; a corrupt or
        # half-written copy must degrade to a warning, not abort the whole run.
        import tasks.shared as cli
        with unittest.mock.patch.object(
            cli, "_merge_verify_module",
            side_effect=SyntaxError("invalid syntax"),
        ):
            issues = _merge_verify_issues({"merge_verify": {"command": "x"}})
        self.assertEqual(len(issues), 1)
        self.assertIn("could not be validated", issues[0])

    def test_missing_runner_is_silent(self):
        import tasks.shared as cli
        with unittest.mock.patch.object(cli, "_merge_verify_module", return_value=None):
            self.assertEqual(_merge_verify_issues({"merge_verify": {"command": "x"}}), [])


class TestUntrackedPolicyAdvisory(unittest.TestCase):
    """A verify command only works if every clone sees it, so an untracked
    declaration is worth mentioning — but only where tracking is a real notion."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {"merge_verify": {"command": "make test"}}

    def test_warns_when_declared_but_untracked(self):
        _git(self.root, "init", "-q")
        _write_config(self.root, self.cfg)
        issues = _merge_verify_untracked(self.root, self.cfg)
        self.assertEqual(len(issues), 1)
        self.assertIn("untracked", issues[0])

    def test_silent_when_tracked(self):
        _git(self.root, "init", "-q")
        _write_config(self.root, self.cfg)
        _git(self.root, "add", ".agent/config.json")
        self.assertEqual(_merge_verify_untracked(self.root, self.cfg), [])

    def test_silent_outside_a_git_work_tree(self):
        # The dogfooding workspace is not itself a repo; "other clones" would be
        # a meaningless warning there.
        _write_config(self.root, self.cfg)
        self.assertEqual(_merge_verify_untracked(self.root, self.cfg), [])

    def test_silent_when_nothing_declared(self):
        _git(self.root, "init", "-q")
        self.assertEqual(_merge_verify_untracked(self.root, {}), [])


class TestMergeDoctorPolicyExemption(unittest.TestCase):
    """A committed config.json is how `merge_verify` reaches every clone, so
    merge-doctor must not demand `git rm --cached` on it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self._build_merge()

    def _build_merge(self):
        r = self.root
        _git(r, "init", "-qb", "main")
        (r / ".agent" / "u1").mkdir(parents=True)
        (r / "MIND_MAP.md").write_text("[1] **Node** - x\n", encoding="utf-8")
        (r / ".agent" / "u1" / "chat_log.md").write_text("log\n", encoding="utf-8")
        (r / "app.py").write_text("code\n", encoding="utf-8")
        _write_config(r, {"merge_verify": {"command": "true"}})
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "base")
        _git(r, "checkout", "-qb", "feature")
        (r / "app.py").write_text("code\nmore\n", encoding="utf-8")
        _git(r, "commit", "-qam", "feat")
        _git(r, "checkout", "-q", "main")
        (r / "MIND_MAP.md").write_text("[1] **Node** - x\n[2] **Two** - y\n", encoding="utf-8")
        _git(r, "commit", "-qam", "main-side")
        _git(r, "merge", "--no-edit", "-q", "feature")

    def _doctor(self):
        """Run merge-doctor, returning (rc, output). rc is the actionable count,
        so assert on the reported paths rather than on a bare number."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_merge_doctor(self.root, "feature", "main")
        return rc, buf.getvalue()

    def test_tracked_config_json_is_not_actionable(self):
        rc, out = self._doctor()
        self.assertEqual(rc, 0, f"merge-doctor flagged a committed policy file:\n{out}")
        self.assertNotIn("config.json", out)

    def test_other_tracked_agent_root_files_are_still_actionable(self):
        # The exemption must be exactly config.json, not a blanket .agent/ hole.
        (self.root / ".agent" / "legacy-notes.md").write_text("stale\n", encoding="utf-8")
        _git(self.root, "add", ".agent/legacy-notes.md")
        _git(self.root, "commit", "-qm", "stale")
        rc, out = self._doctor()
        self.assertGreater(rc, 0)
        self.assertIn(".agent/legacy-notes.md", out)
        # ...and the exempt file is still absent from the findings.
        self.assertNotIn("config.json", out)

    def test_exemption_set_is_narrow(self):
        self.assertEqual(set(SHARED_POLICY_PATHS), {".agent/config.json"})


class TestSkillLiteralFence(unittest.TestCase):
    """Regression fence for the defect this task fixed: the skill must not name
    one project's directory layout or test runner."""

    def test_no_hardcoded_project_literals_in_skill(self):
        text = _SKILL_MD.read_text(encoding="utf-8").lower()
        for literal in ("backend", "run-backend-tests"):
            self.assertNotIn(
                literal, text,
                f"SKILL.md must not hardcode {literal!r} — the verification has to "
                f"work on repos that have no such directory or command",
            )

    def test_skill_documents_the_config_channel(self):
        text = _SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("merge_verify", text)
        self.assertIn(".agent/config.json", text)

    def test_identity_diff_scope_is_pinned_verbatim(self):
        # Substring-checking the excludes was too weak: re-scoping the diff to
        # `-- services/` kept every assertion green while shipping a vacuous
        # instruction again. Pin the whole command, and require it root-anchored
        # (`:/` + `,top`) so it can't silently narrow when run from a subdir.
        self.assertIn(IDENTITY_DIFF, _SKILL_MD.read_text(encoding="utf-8"))

    def test_the_fixture_executes_the_documented_diff(self):
        # The fixture hand-copies the recipe; if the two drift, the fixture
        # proves something the skill no longer tells anyone to run.
        fixture = (_HERE / "merge-verify-fixture.sh").read_text(encoding="utf-8")
        self.assertIn(IDENTITY_DIFF_PATHSPECS, fixture)

    def test_runner_ships_with_the_skill(self):
        self.assertTrue(_MERGE_VERIFY.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
