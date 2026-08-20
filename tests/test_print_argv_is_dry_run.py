#!/usr/bin/env python3
"""`--print-argv` is a DRY RUN — it must never execute the agent.

Field finding (1.5.31 audit): `--print-argv` was declared "instead of
executing — inspectable containment", but its check sat AFTER the `--prompt`
branch in provider.sandbox._main. So `sandbox --print-argv --prompt "..."`
silently ignored the dry-run flag and launched a live, billable agent with the
project writable — the exact opposite of the flag's contract, on the path the
`tasks` help text advertises as the primary one (`bin/sandbox --prompt "..."`).

Inspection flags must short-circuit BEFORE any side-effecting branch. This is
the decision fixture for that ordering: every inspection flag is asserted to
exit without running the agent, with `--prompt` present and absent.

Hermetic: a fake `claude` on PATH stands in for the real CLI, so a regression
prints the fake's marker instead of spending money on a real run.

Run: python3 tests/test_print_argv_is_dry_run.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(PLUGIN))

from provider import sandbox, subagent  # noqa: E402

MARKER = "FAKE_AGENT_WAS_EXECUTED"


class PrintArgvNeverExecutes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)
        # A fake `claude` that announces itself if anything actually runs it.
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        fake = self.bindir / "claude"
        fake.write_text(f"#!/bin/sh\necho {MARKER}\n", encoding="utf-8")
        fake.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _containment_diag(self, allowed: Path) -> str:
        """The containment the --ro-project prompt path actually generates for
        this scenario, rendered into the assertion message so a failure (esp. on
        the macOS seatbelt backend, which no Linux CI leg exercises) reports the
        real profile/argv delta instead of an opaque boolean mismatch."""
        import platform
        try:
            if platform.system() == "Darwin":
                body = sandbox.build_seatbelt_profile(
                    self.proj, sandbox._git_dir_of(self.proj), [str(allowed)],
                    project_writable=False)
                header = "generated seatbelt profile (project_writable=False)"
            else:
                body = "\n".join(sandbox._wrapped_argv(
                    "claude", ["<agent-argv>"], self.proj, [str(allowed)],
                    project_writable=False))
                header = f"generated wrapped argv ({platform.system()} backend)"
        except Exception as exc:  # diagnostics must never mask the real failure
            body, header = f"<could not build containment: {exc!r}>", "containment"
        listing = sorted(p.name for p in self.proj.iterdir())
        return (f"\n--- {header} ---\n{body}\n"
                f"--- project root now contains: {listing} ---")

    def _run(self, *flags: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "PLAYBOOK_SANDBOXED"}
        env["PYTHONPATH"] = str(PLUGIN)
        # Fake agent first so shutil.which resolves it, real tools still reachable.
        env["PATH"] = f"{self.bindir}:{env.get('PATH', '')}"
        return subprocess.run(
            [sys.executable, "-m", "provider.sandbox",
             "--project-root", str(self.proj), *flags],
            env=env, capture_output=True, text=True, timeout=120)

    def test_print_argv_with_prompt_does_not_execute(self):
        r = self._run("--print-argv", "--agent", "claude", "--prompt", "hello")
        self.assertNotIn(MARKER, r.stdout + r.stderr,
                         "--print-argv with --prompt EXECUTED the agent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude", r.stdout, "argv should name the agent binary")

    def test_print_argv_with_prompt_shows_the_prompt_paths_argv(self):
        """The printed argv must be the one the --prompt path would run,
        not the empty raw-passthrough argv (that would be a lie, not a dry run)."""
        r = self._run("--print-argv", "--agent", "claude", "--prompt", "hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        # headless_argv adds the non-interactive flag; a raw `_wrapped_argv([])`
        # dry run would omit it entirely.
        self.assertIn("-p", r.stdout.split(),
                      f"argv is not the headless --prompt invocation:\n{r.stdout}")

    @unittest.skipUnless(shutil.which("bwrap") or sys.platform == "darwin",
                         "no containment backend installed")
    def test_print_argv_stays_contained(self):
        r = self._run("--print-argv", "--agent", "claude", "--prompt", "hello")
        self.assertTrue(
            "bwrap" in r.stdout or "sandbox-exec" in r.stdout,
            f"dry-run argv is not wrapped in containment:\n{r.stdout}")

    def test_print_argv_without_prompt_still_works(self):
        r = self._run("--print-argv", "--agent", "claude", "--", "echo", "hi")
        self.assertNotIn(MARKER, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("bwrap") or sys.platform == "darwin",
                         "no containment backend installed")
    def test_ro_project_prompt_denies_project_write_but_keeps_rw_exception(self):
        """Exercise the real wrapper, not only the SubagentSpec handoff."""
        allowed = self.proj / "allowed"
        allowed.mkdir()
        fake = self.bindir / "claude"
        fake.write_text(
            "#!/bin/sh\n"
            "printf blocked > \"$PWD/blocked\" 2>/dev/null || :\n"
            "printf allowed > \"$PWD/allowed/wrote\" 2>/dev/null || :\n"
            f"echo {MARKER}\n",
            encoding="utf-8",
        )
        r = self._run(
            "--agent", "claude", "--ro-project", "--rw", str(allowed),
            "--prompt", "hello",
        )
        # When this fires on Darwin (the seatbelt backend) it must name the real
        # delta, not just a boolean: print the exact containment the runner
        # generates for this scenario so a headless CI run is diagnosable.
        diag = self._containment_diag(allowed)
        self.assertEqual(r.returncode, 0, r.stderr + diag)
        self.assertNotIn("blocked", [p.name for p in self.proj.iterdir()],
                         "--ro-project prompt execution wrote the project root" + diag)
        wrote = allowed / "wrote"
        self.assertTrue(wrote.exists(),
                        "the --rw exception never received the write" + diag)
        self.assertEqual(wrote.read_text(encoding="utf-8"), "allowed", diag)

    def test_other_inspection_flags_never_execute(self):
        for flags in (("--list-agents",), ("--list-models",),
                      ("--print-profile",),
                      ("--print-profile", "--prompt", "hello"),
                      ("--list-agents", "--prompt", "hello"),
                      ("--list-models", "--prompt", "hello")):
            with self.subTest(flags=flags):
                r = self._run(*flags)
                self.assertNotIn(MARKER, r.stdout + r.stderr,
                                 f"{flags} EXECUTED the agent")
                self.assertEqual(r.returncode, 0, r.stderr)


class PromptContainmentMatchesTheCliFlags(unittest.TestCase):
    """The inspected wrapper and the executed prompt must enforce one policy."""

    def _capture_spec(self, *extra: str):
        captured = []

        def fake_run(spec, *, project_root):
            captured.append(spec)
            return subagent.SubagentResult(text="stub", returncode=0)

        def fake_stream(spec, *, project_root):
            captured.append(spec)
            return iter(())

        with mock.patch.object(subagent, "run_subagent", side_effect=fake_run), \
                mock.patch.object(subagent, "stream_subagent", side_effect=fake_stream), \
                redirect_stdout(StringIO()):
            rc = sandbox._main([
                "--agent", "grok", "--project-root", str(_HERE.parent),
                "--ro-project", "--rw", "/tmp/one", "--rw", "/tmp/two",
                "--prompt", "inspect", *extra,
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_read_only_and_rw_paths_reach_prompt_runner(self):
        spec = self._capture_spec()
        self.assertEqual(spec.contain, "outdir")
        self.assertEqual(tuple(map(str, spec.extra_rw)), ("/tmp/one", "/tmp/two"))

    def test_read_only_and_rw_paths_reach_streaming_prompt_runner(self):
        spec = self._capture_spec("--stream")
        self.assertEqual(spec.contain, "outdir")
        self.assertEqual(tuple(map(str, spec.extra_rw)), ("/tmp/one", "/tmp/two"))


# ── macOS seatbelt --ro-project containment, proven at the PROFILE-TEXT level ─
#
# Confirmed live on macOS CI (run 32381370391, job 96465123480):
#
#   AssertionError: 'blocked' unexpectedly found in ['allowed', '.git', 'blocked']
#     : --ro-project prompt execution wrote the project root
#
# `build_seatbelt_profile` expressed the read-only project ONLY as the ABSENCE
# of a `(require-not (subpath "<project>"))` inside one `(require-all …)` deny
# block. `require-all` of `require-not`s denies a write only where NO exemption
# matches — so any BROADER exemption cancels it. `_SYSTEM_RW_PATHS` includes
# `/var/folders` (and `/private/var/folders`, `/tmp`, …), and macOS `mktemp`
# places directories under `/var/folders`; a project rooted there matches
# `(require-not (subpath "/var/folders"))`, so `require-all` is false for every
# write under the project and nothing denies it — the project was fully writable
# despite `--ro-project`. The `--rw` exception (which lives inside the block the
# same way) still worked, which is why the CI failure allowed `allowed` but not
# `blocked`.
#
# The working precedent in the same function is the `.git` deny: an
# unconditional TERMINAL `(deny file-write* (subpath …))` appended after the
# exemption block — seatbelt applies the LAST matching rule. The fix expresses
# the read-only project the same way, then re-allows each `extra_rw` workspace
# after it so a writable workspace inside the read-only project keeps access.
#
# These run on Linux even though seatbelt cannot execute here: they read the
# generated profile text (literal-string assertions, so a reader can SEE why it
# denies) and evaluate it with a small "last matching rule wins" seatbelt
# interpreter. That proves the POLICY is correct; the end-to-end proof that
# macOS ENFORCES it is the live-platform test above,
# `test_ro_project_prompt_denies_project_write_but_keeps_rw_exception`, which
# runs under real seatbelt on macOS CI only.


def _under(path: str, base: str) -> bool:
    base = base.rstrip("/")
    return path == base or path.startswith(base + "/")


def seatbelt_write_decision(profile: str, path: str) -> str:
    """Return 'allow' or 'deny' for a file-write to `path` under `profile`.

    Models enough of SBPL to evaluate what build_seatbelt_profile emits:
    `(allow default)`, one `(deny file-write* (require-all (require-not …)))`
    block, and terminal `(deny|allow file-write* (subpath "…"))` rules. macOS
    applies the LAST matching rule; we mirror that so a Linux run proves what
    the real kernel would decide.
    """
    require_nots: list[tuple[str, str]] = []  # (kind, value): "subpath"|"regex"
    terminals: list[tuple[str, str]] = []     # (effect, subpath): "allow"|"deny"
    in_require_all = False
    for raw in profile.splitlines():
        line = raw.strip()
        if line.startswith("(require-all"):
            in_require_all = True
            continue
        if in_require_all:
            m = re.match(r'\(require-not \(subpath "(.+)"\)\)', line)
            if m:
                require_nots.append(("subpath", m.group(1)))
                continue
            m = re.match(r'\(require-not \(regex #"(.+)"\)\)', line)
            if m:
                require_nots.append(("regex", m.group(1)))
                continue
            if line.startswith(")"):
                in_require_all = False
            continue
        m = re.match(r'\((deny|allow) file-write\* \(subpath "(.+)"\)\)', line)
        if m:
            terminals.append((m.group(1), m.group(2)))

    def _matches(kind: str, value: str) -> bool:
        return _under(path, value) if kind == "subpath" else re.search(value, path) is not None

    decision = "allow"  # (allow default)
    if require_nots and not any(_matches(k, v) for k, v in require_nots):
        decision = "deny"  # the require-all block denies where no exemption matches
    for effect, subpath in terminals:  # last matching rule wins
        if _under(path, subpath):
            decision = effect
    return decision


class SeatbeltPolicyInterpreterSanity(unittest.TestCase):
    """The interpreter must model 'last matching rule wins', or the policy
    assertions below would prove nothing."""

    def test_last_matching_rule_wins(self):
        prof = ('(allow default)\n'
                '(deny file-write* (subpath "/a"))\n'
                '(allow file-write* (subpath "/a/b"))')
        self.assertEqual("deny", seatbelt_write_decision(prof, "/a/x"))
        self.assertEqual("allow", seatbelt_write_decision(prof, "/a/b/x"))

    def test_require_all_block_denies_only_where_no_exemption_matches(self):
        prof = ('(allow default)\n'
                '(deny file-write*\n    (require-all\n'
                '        (require-not (subpath "/tmp"))\n    )\n)')
        self.assertEqual("deny", seatbelt_write_decision(prof, "/etc/x"))
        self.assertEqual("allow", seatbelt_write_decision(prof, "/tmp/x"))


# Every temp root macOS mktemp / _SYSTEM_RW_PATHS can put a project under, plus a
# non-temp home-side root (the case that already worked) as a control.
_SEATBELT_PROJECT_ROOTS = [
    f"{p}/xy/abc123/T/corpus-proj" for p in sandbox._SYSTEM_RW_PATHS
] + ["/Users/ci/work/corpus-proj"]


class RoProjectSeatbeltProfileDeniesWrites(unittest.TestCase):
    """project_writable=False must deny writes to the project subpath — for a
    project rooted under ANY of _SYSTEM_RW_PATHS, not only outside them."""

    def _profile(self, project, extra_rw=None):
        return sandbox.build_seatbelt_profile(
            project, project + "/.git", extra_rw, project_writable=False)

    def test_literal_terminal_deny_of_project_is_emitted(self):
        # Literal-string assertion: a reader must be able to SEE why it denies.
        for project in _SEATBELT_PROJECT_ROOTS:
            with self.subTest(project=project):
                profile = self._profile(project)
                self.assertIn(
                    f'(deny file-write* (subpath "{project}"))', profile,
                    "the read-only project must be an explicit terminal deny, not "
                    "merely an omitted require-not exemption:\n" + profile)

    def test_project_write_is_denied_under_every_system_rw_root(self):
        for project in _SEATBELT_PROJECT_ROOTS:
            with self.subTest(project=project):
                profile = self._profile(project)
                self.assertEqual(
                    "deny",
                    seatbelt_write_decision(profile, project + "/pwned.txt"),
                    "a --ro-project write to the project root must be denied even "
                    "when the project lives under a system rw path:\n" + profile)

    def test_git_stays_denied_in_ro_mode(self):
        for project in _SEATBELT_PROJECT_ROOTS:
            with self.subTest(project=project):
                profile = self._profile(project)
                self.assertEqual(
                    "deny",
                    seatbelt_write_decision(profile, project + "/.git/config"),
                    ".git must stay read-only:\n" + profile)

    def test_the_hosting_system_path_itself_stays_writable(self):
        # The temp root must stay writable OUTSIDE the project — the agent binary
        # and mktemp need it. Only the project subtree becomes read-only.
        for sys_path in sandbox._SYSTEM_RW_PATHS:
            project = f"{sys_path}/xy/abc123/T/corpus-proj"
            with self.subTest(sys_path=sys_path):
                profile = self._profile(project)
                sibling = f"{sys_path}/xy/abc123/T/other-scratch"
                self.assertEqual(
                    "allow", seatbelt_write_decision(profile, sibling),
                    f"{sys_path} must stay writable outside the project:\n" + profile)

    def test_extra_rw_workspace_inside_ro_project_stays_writable(self):
        # The normal workspace case: the --rw dir lives inside the project.
        for project in _SEATBELT_PROJECT_ROOTS:
            ws = project + "/allowed"
            with self.subTest(project=project):
                profile = self._profile(project, extra_rw=[ws])
                self.assertEqual(
                    "allow", seatbelt_write_decision(profile, ws + "/wrote"),
                    "the --rw workspace inside a read-only project must stay "
                    "writable:\n" + profile)
                self.assertEqual(
                    "deny", seatbelt_write_decision(profile, project + "/pwned.txt"),
                    "only extra_rw is writable inside a read-only project:\n" + profile)
                # The re-allow must textually FOLLOW the project deny (last wins).
                self.assertLess(
                    profile.index(f'(deny file-write* (subpath "{project}"))'),
                    profile.index(f'(allow file-write* (subpath "{ws}"))'),
                    "the extra_rw re-allow must come AFTER the project deny:\n" + profile)


class WorkerModeSeatbeltProfileUnchanged(unittest.TestCase):
    """The complement: project_writable=True keeps the project writable, and in
    BOTH modes an extra_rw path inside the project stays writable."""

    def test_project_stays_writable_in_worker_mode(self):
        for project in _SEATBELT_PROJECT_ROOTS:
            with self.subTest(project=project):
                profile = sandbox.build_seatbelt_profile(
                    project, project + "/.git", None, project_writable=True)
                self.assertNotIn(
                    f'(deny file-write* (subpath "{project}"))', profile,
                    "worker mode must not deny the project:\n" + profile)
                self.assertEqual(
                    "allow", seatbelt_write_decision(profile, project + "/edit.txt"),
                    "worker mode must keep the project writable:\n" + profile)
                self.assertEqual(
                    "deny", seatbelt_write_decision(profile, project + "/.git/config"),
                    ".git must stay read-only even in worker mode:\n" + profile)

    def test_extra_rw_inside_project_writable_in_both_modes(self):
        project = "/Users/ci/work/corpus-proj"
        ws = project + "/allowed"
        for writable in (True, False):
            with self.subTest(project_writable=writable):
                profile = sandbox.build_seatbelt_profile(
                    project, project + "/.git", [ws], project_writable=writable)
                self.assertEqual(
                    "allow", seatbelt_write_decision(profile, ws + "/wrote"),
                    f"extra_rw must stay writable (project_writable={writable}):\n"
                    + profile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
